"""归档/销毁执行引擎。

语义：
- 分批推进：每批最多 batch_size 个文档，按 doc_id 游标扫描，不全量加载；
- 并发执行：批内由线程池处理，单个文档失败不影响同批其他文档；
- 可恢复：归档采用"先写归档文件（原子 rename），再提交状态"的顺序，
  崩溃只会留下 ARCHIVING 中间态，重启后 recover_inflight() 回滚为 ACTIVE
  重新处理（归档写入幂等）；PENDING_DESTRUCTION 的销毁操作本身幂等，
  下一次销毁扫描会接着完成；
- 可取消/超时：取消或单文档超时都会把文档回滚到 ACTIVE，
  不会停留在半归档状态。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from .models import (ContentCorruptError, ContentMissingError, Document,
                     DocumentStatus, FailureReason, ProcessResult)


class ArchiveEngine:
    def __init__(self, store, archive_store, holds,
                 batch_size: int = 100, workers: int = 4,
                 doc_timeout: float = 30.0) -> None:
        self._store = store
        self._archive = archive_store
        self._holds = holds
        self._batch_size = batch_size
        self._workers = workers
        self._doc_timeout = doc_timeout
        self._cancel = threading.Event()
        self._run_lock = threading.Lock()  # 同一类 pass 不并发重入

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """请求取消：不再认领新文档，处理中的文档回滚到安全状态。"""
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def recover_inflight(self) -> int:
        """进程重启后回收上次未提交的 in-flight 文档，返回回收条数。"""
        count = 0
        for doc in self._store.scan_inflight():
            if self._store.transition(doc.doc_id, DocumentStatus.ARCHIVING,
                                      DocumentStatus.ACTIVE):
                count += 1
        return count

    def run_archive_pass(self, now: Optional[float] = None) -> List[ProcessResult]:
        """归档所有到期文档，返回本批处理结果。"""
        now = time.time() if now is None else now
        results: List[ProcessResult] = []
        with self._run_lock:
            cursor: Optional[str] = None
            while not self._cancel.is_set():
                batch = self._store.scan_due(now, self._batch_size, cursor)
                if not batch:
                    break
                cursor = batch[-1].doc_id
                results.extend(self._run_batch(
                    batch, lambda d: self._archive_one(d)))
        return results

    def run_destroy_pass(self, now: Optional[float] = None) -> List[ProcessResult]:
        """销毁所有已归档（含上次未完成的待销毁）文档。"""
        results: List[ProcessResult] = []
        with self._run_lock:
            cursor: Optional[str] = None
            while not self._cancel.is_set():
                batch = self._store.scan_archived(self._batch_size, cursor)
                if not batch:
                    break
                cursor = batch[-1].doc_id
                results.extend(self._run_batch(
                    batch, lambda d: self._destroy_one(d)))
        return results

    # ------------------------------------------------------------------
    # 批内并发
    # ------------------------------------------------------------------

    def _run_batch(self, batch: List[Document], fn) -> List[ProcessResult]:
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            return list(pool.map(fn, batch))

    # ------------------------------------------------------------------
    # 单文档处理
    # ------------------------------------------------------------------

    def _deadline_exceeded(self, start: float) -> bool:
        return time.monotonic() - start > self._doc_timeout

    def _archive_one(self, doc: Document) -> ProcessResult:
        start = time.monotonic()
        doc_id = doc.doc_id
        # 认领：并发下只有一个执行者能把 ACTIVE -> ARCHIVING
        if not self._store.transition(doc_id, DocumentStatus.ACTIVE,
                                      DocumentStatus.ARCHIVING):
            return self._record("archive", ProcessResult(
                doc_id, False, FailureReason.INVALID_STATE, "claim failed"))
        try:
            content = self._store.get_content(doc_id)
        except KeyError:
            self._rollback_to_active(doc_id)
            return self._record("archive", ProcessResult(
                doc_id, False, FailureReason.CONTENT_MISSING,
                "source content missing"))
        if self._cancel.is_set():
            self._rollback_to_active(doc_id)
            return self._record("archive", ProcessResult(
                doc_id, False, FailureReason.CANCELLED, "cancelled"))
        if self._deadline_exceeded(start):
            self._rollback_to_active(doc_id)
            return self._record("archive", ProcessResult(
                doc_id, False, FailureReason.TIMEOUT, "deadline exceeded"))
        try:
            self._archive.put(doc_id, content, {
                "doc_id": doc_id, "policy_id": doc.policy_id,
                "archived_at": time.time()})
        except OSError as exc:  # 归档介质错误：回滚，可重试
            self._rollback_to_active(doc_id)
            return self._record("archive", ProcessResult(
                doc_id, False, FailureReason.INTERNAL, str(exc)))
        if self._cancel.is_set() or self._deadline_exceeded(start):
            # 提交前最后检查：取消/超时不得留下半归档状态
            self._rollback_to_active(doc_id)
            reason = (FailureReason.CANCELLED if self._cancel.is_set()
                      else FailureReason.TIMEOUT)
            return self._record("archive", ProcessResult(
                doc_id, False, reason, "aborted before commit"))
        # 提交：状态推进到 ARCHIVED 与 archive_id 绑定在同一原子更新中
        if not self._store.transition(doc_id, DocumentStatus.ARCHIVING,
                                      DocumentStatus.ARCHIVED,
                                      archive_id=doc_id):
            self._rollback_to_active(doc_id)
            return self._record("archive", ProcessResult(
                doc_id, False, FailureReason.INVALID_STATE,
                "commit lost race"))
        return self._record("archive", ProcessResult(doc_id, True))

    def _destroy_one(self, doc: Document) -> ProcessResult:
        doc_id = doc.doc_id
        # 法务保全：排除在自动销毁之外，状态不动，解除后自然重进进度
        if self._holds.is_held(doc_id):
            return self._record("destroy", ProcessResult(
                doc_id, False, FailureReason.LEGAL_HOLD, "legal hold active"))
        # 销毁前必须能校验归档内容，缺失/损坏时拒绝销毁并报可区分错误
        archive_id = doc.archive_id or doc_id
        try:
            self._archive.read_verified(archive_id)
        except ContentMissingError:
            return self._record("destroy", ProcessResult(
                doc_id, False, FailureReason.CONTENT_MISSING,
                "archive missing, destroy refused"))
        except ContentCorruptError:
            return self._record("destroy", ProcessResult(
                doc_id, False, FailureReason.CONTENT_CORRUPT,
                "archive corrupt, destroy refused"))
        if self._cancel.is_set():
            return self._record("destroy", ProcessResult(
                doc_id, False, FailureReason.CANCELLED, "cancelled"))
        # 进入待销毁；若已是 PENDING_DESTRUCTION（重启续跑）则直接继续
        if doc.status == DocumentStatus.ARCHIVED:
            if not self._store.transition(
                    doc_id, DocumentStatus.ARCHIVED,
                    DocumentStatus.PENDING_DESTRUCTION):
                return self._record("destroy", ProcessResult(
                    doc_id, False, FailureReason.INVALID_STATE,
                    "claim failed"))
        # 以下删除操作均幂等，崩溃后下次扫描可安全重放
        self._store.delete_content(doc_id)
        self._archive.remove(archive_id)
        if not self._store.transition(doc_id,
                                      DocumentStatus.PENDING_DESTRUCTION,
                                      DocumentStatus.DESTROYED):
            return self._record("destroy", ProcessResult(
                doc_id, False, FailureReason.INVALID_STATE,
                "final transition failed"))
        return self._record("destroy", ProcessResult(doc_id, True))

    # ------------------------------------------------------------------

    def _rollback_to_active(self, doc_id: str) -> None:
        self._store.transition(doc_id, DocumentStatus.ARCHIVING,
                               DocumentStatus.ACTIVE)

    def _record(self, action: str, result: ProcessResult) -> ProcessResult:
        self._store.record_result(action, result)
        return result
