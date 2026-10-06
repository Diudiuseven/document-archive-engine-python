"""归档执行引擎：分批推进、并发执行、取消与超时、检查点恢复。

设计要点：
- 每个文档的处理（状态流转 + 数据搬运）在单个事务中提交，
  取消/超时/失败都会回滚到上一个完整状态，不留半归档中间态。
- 文档级锁防止并发任务重复处理同一文档。
- 每批处理完写入检查点，进程重启后从上次已提交位置继续。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from .errors import (
    DocumentNotFoundError,
    LegalHoldActiveError,
    TaskCancelledError,
    TaskTimeoutError,
)
from .models import BatchResult, Document, DocumentStatus, FailureReason
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
        raise NotImplementedError

    def run_destroy_batch(
        self,
        now: Optional[datetime] = None,
        *,
        token: Optional[CancelToken] = None,
        resume: bool = True,
    ) -> BatchResult:
        """推进一批到期文档销毁。命中保全的文档跳过并回退状态。"""
        raise NotImplementedError

    # ---- 单文档处理（事务粒度） ----
    def archive_document(
        self,
        document_id: str,
        now: datetime,
        *,
        token: Optional[CancelToken] = None,
        deadline: Optional[float] = None,
    ) -> None:
        raise NotImplementedError

    def destroy_document(
        self,
        document_id: str,
        now: datetime,
        *,
        token: Optional[CancelToken] = None,
        deadline: Optional[float] = None,
    ) -> None:
        raise NotImplementedError

    # ---- 归档读取与校验 ----
    def read_archived_content(self, document_id: str) -> bytes:
        """读取归档内容并校验完整性。缺失/损坏抛出可区分错误。"""
        raise NotImplementedError

    # ---- 内部 ----
    def _acquire_doc_lock(self, document_id: str) -> threading.Lock:
        raise NotImplementedError

    def _run_batch(
        self,
        task_name: str,
        status: DocumentStatus,
        due_column: str,
        handler,
        now: datetime,
        token: Optional[CancelToken],
        resume: bool,
    ) -> BatchResult:
        raise NotImplementedError
