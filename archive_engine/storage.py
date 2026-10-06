"""SQLite 持久化层：元数据、内容、归档数据、检查点。

所有多步写操作都在单个事务中提交，读取方只能看到已提交的完整状态。
进程重启后通过检查点表从上次已提交位置继续。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

from .models import Document, DocumentStatus, LegalHold, RetentionPolicy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    policy_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    archive_due_at  TEXT,
    destroy_due_at  TEXT,
    policy_version  INTEGER NOT NULL,
    archived_at     TEXT,
    destroyed_at    TEXT,
    content_checksum TEXT
);
CREATE TABLE IF NOT EXISTS contents (
    document_id TEXT PRIMARY KEY,
    data        BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_blobs (
    document_id TEXT PRIMARY KEY,
    data        BLOB NOT NULL,
    checksum    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id     TEXT PRIMARY KEY,
    active_days   INTEGER NOT NULL,
    archived_days INTEGER NOT NULL,
    version       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_holds (
    hold_id     TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    reason      TEXT NOT NULL,
    placed_at   TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_doc ON legal_holds(document_id);
CREATE TABLE IF NOT EXISTS checkpoints (
    task_name  TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Storage:
    """线程安全的 SQLite 存储。check_same_thread=False + 写锁串行化写事务。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._write_lock = threading.Lock()
        with self._write_lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：提交前读取方看不到中间态。"""
        with self._write_lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()

    # ---- 文档 ----
    def put_document(self, doc: Document, content: bytes) -> None:
        raise NotImplementedError

    def get_document(self, document_id: str) -> Optional[Document]:
        raise NotImplementedError

    def get_content(self, document_id: str) -> Optional[bytes]:
        raise NotImplementedError

    def iter_due_documents(
        self,
        status: DocumentStatus,
        due_column: str,
        now: datetime,
        after_id: str = "",
        limit: int = 100,
    ) -> list[Document]:
        """按游标分页拉取到期文档，不一次性加载全部。"""
        raise NotImplementedError

    # ---- 状态流转（事务内校验并提交） ----
    def transition(
        self,
        document_id: str,
        target: DocumentStatus,
        *,
        expected: Optional[DocumentStatus] = None,
        archived_at: Optional[datetime] = None,
        destroyed_at: Optional[datetime] = None,
        checksum: Optional[str] = None,
    ) -> Document:
        raise NotImplementedError

    # ---- 归档数据 ----
    def write_archive_blob(
        self, document_id: str, data: bytes, checksum: str
    ) -> None:
        raise NotImplementedError

    def read_archive_blob(self, document_id: str) -> tuple[bytes, str]:
        raise NotImplementedError

    def delete_content(self, document_id: str) -> None:
        raise NotImplementedError

    # ---- 策略 ----
    def put_policy(self, policy: RetentionPolicy) -> None:
        raise NotImplementedError

    def get_policy(self, policy_id: str) -> Optional[RetentionPolicy]:
        raise NotImplementedError

    def iter_documents_by_policy(
        self, policy_id: str, after_id: str = "", limit: int = 100
    ) -> list[Document]:
        raise NotImplementedError

    def update_document_schedule(
        self,
        document_id: str,
        archive_due_at: Optional[datetime],
        destroy_due_at: Optional[datetime],
        policy_version: int,
    ) -> None:
        raise NotImplementedError

    # ---- 法务保全 ----
    def place_hold(self, hold: LegalHold) -> None:
        raise NotImplementedError

    def release_hold(self, hold_id: str, released_at: datetime) -> None:
        raise NotImplementedError

    def active_holds(self, document_id: str) -> list[LegalHold]:
        raise NotImplementedError

    # ---- 检查点（重启恢复） ----
    def save_checkpoint(self, task_name: str, cursor: str) -> None:
        raise NotImplementedError

    def load_checkpoint(self, task_name: str) -> Optional[str]:
        raise NotImplementedError
