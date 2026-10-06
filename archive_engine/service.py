"""门面服务：组合存储、策略与引擎，供 API 层与测试使用。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from .engine import ArchiveEngine, CancelToken
from .errors import DocumentNotFoundError
from .models import BatchResult, Document, LegalHold, RetentionPolicy
from .policy import PolicyService
from .storage import Storage


class DocumentArchiveService:
    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        batch_size: int = 50,
        max_workers: int = 4,
        per_document_timeout: float = 30.0,
    ) -> None:
        self.storage = Storage(db_path)
        self.policies = PolicyService(self.storage)
        self.engine = ArchiveEngine(
            self.storage,
            batch_size=batch_size,
            max_workers=max_workers,
            per_document_timeout=per_document_timeout,
        )

    def close(self) -> None:
        self.storage.close()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    # ---- 文档 ----
    def create_document(
        self, policy_id: str, content: bytes, document_id: Optional[str] = None
    ) -> Document:
        raise NotImplementedError

    def get_document(self, document_id: str) -> Document:
        doc = self.storage.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)
        return doc

    def read_archived_content(self, document_id: str) -> bytes:
        return self.engine.read_archived_content(document_id)

    # ---- 法务保全 ----
    def place_hold(self, document_id: str, reason: str) -> LegalHold:
        raise NotImplementedError

    def release_hold(self, hold_id: str) -> None:
        raise NotImplementedError

    # ---- 生命周期推进 ----
    def run_archive_batch(self, token: Optional[CancelToken] = None) -> BatchResult:
        return self.engine.run_archive_batch(self._now(), token=token)

    def run_destroy_batch(self, token: Optional[CancelToken] = None) -> BatchResult:
        return self.engine.run_destroy_batch(self._now(), token=token)
