"""归档执行引擎：分批推进、并发执行、取消与超时、检查点恢复。

设计要点：
- 每个文档的处理（状态流转 + 数据搬运）在单个事务中提交，
  取消/超时/失败都会回滚到上一个完整状态，不留半归档中间态。
- 文档级锁防止并发任务重复处理同一文档。
- 每批处理完写入检查点，进程重启后从上次已提交位置继续。
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

from .errors import (
    ArchiveDataCorruptedError,
    ArchiveDataMissingError,
    ArchiveEngineError,
    DocumentLockedError,
    DocumentNotFoundError,
    InvalidStateTransitionError,
    LegalHoldActiveError,
    TaskCancelledError,
    TaskTimeoutError,
)
from .models import (
    BatchResult,
    Document,
    DocumentFailure,
    DocumentStatus,
    FailureReason,
)
from .storage import Storage

ARCHIVE_TASK = "archive"
DESTROY_TASK = "destroy"


class CancelToken:
    """协作式取消令牌。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise TaskCancelledError("task cancelled")


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failure_reason(exc: BaseException) -> FailureReason:
    if isinstance(exc, TaskCancelledError):
        return FailureReason.CANCELLED
    if isinstance(exc, TaskTimeoutError):
        return FailureReason.TIMEOUT
    if isinstance(exc, DocumentLockedError):
        return FailureReason.LOCKED
    if isinstance(exc, LegalHoldActiveError):
        return FailureReason.LEGAL_HOLD
    if isinstance(exc, ArchiveDataMissingError):
        return FailureReason.DATA_MISSING
    if isinstance(exc, ArchiveDataCorruptedError):
        return FailureReason.DATA_CORRUPTED
    if isinstance(exc, InvalidStateTransitionError):
        return FailureReason.INVALID_STATE
    return FailureReason.INTERNAL


class ArchiveEngine:
    def __init__(
        self,
        storage: Storage,
        *,
        batch_size: int = 50,
        max_workers: int = 4,
        per_document_timeout: float = 30.0,
    ) -> None:
        self._storage = storage
        self._batch_size = batch_size
        self._max_workers = max_workers
        self._timeout = per_document_timeout
        self._doc_locks: dict[str, threading.Lock] = {}
        self._doc_locks_guard = threading.Lock()

    # ---- 对外入口 ----
    def run_archive_batch(
        self,
        now: Optional[datetime] = None,
        *,
        token: Optional[CancelToken] = None,
        resume: bool = True,
    ) -> BatchResult:
        """推进一批到期文档进入归档。失败不拖垮整批。"""
        now = now or datetime.now(timezone.utc)
        return self._run_batch(
            ARCHIVE_TASK,
            DocumentStatus.ACTIVE,
            "archive_due_at",
            lambda doc_id, deadline: self.archive_document(
                doc_id, now, token=token, deadline=deadline
            ),
            now,
            token,
            resume,
        )

    def run_destroy_batch(
        self,
        now: Optional[datetime] = None,
        *,
        token: Optional[CancelToken] = None,
        resume: bool = True,
    ) -> BatchResult:
        """推进一批到期文档销毁。命中保全的文档跳过并回退状态。"""
        now = now or datetime.now(timezone.utc)
        return self._run_batch(
            DESTROY_TASK,
            DocumentStatus.ARCHIVED,
            "destroy_due_at",
            lambda doc_id, deadline: self.destroy_document(
                doc_id, now, token=token, deadline=deadline
            ),
            now,
            token,
            resume,
        )

    # ---- 单文档处理（事务粒度） ----
    def archive_document(
        self,
        document_id: str,
        now: datetime,
        *,
        token: Optional[CancelToken] = None,
        deadline: Optional[float] = None,
    ) -> None:
        lock = self._acquire_doc_lock(document_id)
        if not lock.acquire(blocking=False):
            raise DocumentLockedError(document_id)
        try:
            self._check_cancel_and_deadline(token, deadline)
            # ACTIVE -> ARCHIVING（事务内校验当前状态）
            self._storage.transition(
                document_id, DocumentStatus.ARCHIVING,
                expected=DocumentStatus.ACTIVE,
            )
            try:
                content = self._storage.get_content(document_id)
                if content is None:
                    raise ArchiveDataMissingError(
                        f"content missing for document {document_id!r}"
                    )
                digest = _checksum(content)
                self._check_cancel_and_deadline(token, deadline)
                # 单事务提交：写归档数据 + 删原文 + 状态 -> ARCHIVED
                self._storage.complete_archive(document_id, content, digest, now)
            except BaseException:
                # 取消/超时/失败一律回滚到 ACTIVE，不留半归档中间态
                self._rollback(document_id, DocumentStatus.ACTIVE)
                raise
        finally:
            lock.release()

    def destroy_document(
        self,
        document_id: str,
        now: datetime,
        *,
        token: Optional[CancelToken] = None,
        deadline: Optional[float] = None,
    ) -> None:
        lock = self._acquire_doc_lock(document_id)
        if not lock.acquire(blocking=False):
            raise DocumentLockedError(document_id)
        try:
            self._check_cancel_and_deadline(token, deadline)
            # ARCHIVED -> PENDING_DESTRUCTION
            self._storage.transition(
                document_id, DocumentStatus.PENDING_DESTRUCTION,
                expected=DocumentStatus.ARCHIVED,
            )
            try:
                # 法务保全拦截：命中则回退到 ARCHIVED，不销毁
                holds = self._storage.active_holds(document_id)
                if holds:
                    raise LegalHoldActiveError(
                        f"document {document_id!r} under {len(holds)} active hold(s)"
                    )
                self._check_cancel_and_deadline(token, deadline)
                # 单事务提交：删归档数据 + 状态 -> DESTROYED
                self._storage.complete_destroy(document_id, now)
            except BaseException:
                # 回退到 ARCHIVED，保全解除后可重新进入销毁进度
                self._rollback(document_id, DocumentStatus.ARCHIVED)
                raise
        finally:
            lock.release()

    # ---- 归档读取与校验 ----
    def read_archived_content(self, document_id: str) -> bytes:
        """读取归档内容并校验完整性。缺失/损坏抛出可区分错误。"""
        doc = self._storage.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)
        if doc.status not in (
            DocumentStatus.ARCHIVED,
            DocumentStatus.PENDING_DESTRUCTION,
        ):
            raise InvalidStateTransitionError(
                document_id, doc.status.value, "read_archived"
            )
        data, stored_checksum = self._storage.read_archive_blob(document_id)
        actual = _checksum(data)
        if actual != stored_checksum:
            raise ArchiveDataCorruptedError(
                f"archive blob checksum mismatch for {document_id!r}"
            )
        if doc.content_checksum and actual != doc.content_checksum:
            raise ArchiveDataCorruptedError(
                f"archive content diverged from recorded checksum for {document_id!r}"
            )
        return data

    # ---- 内部 ----
    def _acquire_doc_lock(self, document_id: str) -> threading.Lock:
        with self._doc_locks_guard:
            lock = self._doc_locks.get(document_id)
            if lock is None:
                lock = threading.Lock()
                self._doc_locks[document_id] = lock
            return lock

    @staticmethod
    def _check_cancel_and_deadline(
        token: Optional[CancelToken], deadline: Optional[float]
    ) -> None:
        if token is not None:
            token.throw_if_cancelled()
        if deadline is not None and time.monotonic() > deadline:
            raise TaskTimeoutError("per-document processing timeout")

    def _rollback(self, document_id: str, target: DocumentStatus) -> None:
        """尽力回滚到处理前状态；目标状态必须在允许流转表内。"""
        try:
            self._storage.transition(document_id, target)
        except ArchiveEngineError:
            pass

    def _run_batch(
        self,
        task_name: str,
        status: DocumentStatus,
        due_column: str,
        handler: Callable[[str, Optional[float]], None],
        now: datetime,
        token: Optional[CancelToken],
        resume: bool,
    ) -> BatchResult:
        result = BatchResult()
        if token is not None and token.cancelled:
            result.cancelled = True
            return result

        cursor = self._storage.load_checkpoint(task_name) if resume else None
        after_id = cursor or ""
        docs = self._storage.iter_due_documents(
            status, due_column, now, after_id=after_id, limit=self._batch_size
        )
        if not docs:
            if after_id:
                # 游标已扫到末尾，重置以便下一轮从头扫描
                self._storage.save_checkpoint(task_name, "")
            return result

        deadline = time.monotonic() + self._timeout
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(handler, doc.document_id, deadline): doc
                for doc in docs
            }
            for future in as_completed(futures):
                doc = futures[future]
                try:
                    future.result()
                    result.processed.append(doc.document_id)
                except BaseException as exc:  # 单文档失败不拖垮整批
                    result.failures.append(
                        DocumentFailure(
                            document_id=doc.document_id,
                            reason=_failure_reason(exc),
                            detail=str(exc),
                        )
                    )
        # 提交检查点：重启后从本批末尾继续；扫描完成则重置游标，
        # 避免漏掉游标之前但当时未到期/新加入的文档。
        if len(docs) < self._batch_size:
            self._storage.save_checkpoint(task_name, "")
        else:
            self._storage.save_checkpoint(task_name, docs[-1].document_id)
        if token is not None and token.cancelled:
            result.cancelled = True
        return result
