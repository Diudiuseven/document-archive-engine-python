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

from .errors import (
    ArchiveDataMissingError,
    DocumentNotFoundError,
    InvalidStateTransitionError,
)
from .models import ALLOWED_TRANSITIONS, Document, DocumentStatus, LegalHold, RetentionPolicy

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


def _dt_to_str(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _dt_from_str(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _row_to_document(row: sqlite3.Row) -> Document:
    return Document(
        document_id=row["document_id"],
        policy_id=row["policy_id"],
        status=DocumentStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        archive_due_at=_dt_from_str(row["archive_due_at"]),
        destroy_due_at=_dt_from_str(row["destroy_due_at"]),
        policy_version=row["policy_version"],
        archived_at=_dt_from_str(row["archived_at"]),
        destroyed_at=_dt_from_str(row["destroyed_at"]),
        content_checksum=row["content_checksum"],
    )


def _row_to_hold(row: sqlite3.Row) -> LegalHold:
    return LegalHold(
        hold_id=row["hold_id"],
        document_id=row["document_id"],
        reason=row["reason"],
        placed_at=datetime.fromisoformat(row["placed_at"]),
        released_at=_dt_from_str(row["released_at"]),
    )


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
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO documents
                   (document_id, policy_id, status, created_at, archive_due_at,
                    destroy_due_at, policy_version, archived_at, destroyed_at,
                    content_checksum)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc.document_id, doc.policy_id, doc.status.value,
                    doc.created_at.isoformat(), _dt_to_str(doc.archive_due_at),
                    _dt_to_str(doc.destroy_due_at), doc.policy_version,
                    _dt_to_str(doc.archived_at), _dt_to_str(doc.destroyed_at),
                    doc.content_checksum,
                ),
            )
            conn.execute(
                "INSERT INTO contents (document_id, data) VALUES (?,?)",
                (doc.document_id, content),
            )

    def get_document(self, document_id: str) -> Optional[Document]:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE document_id=?", (document_id,)
        ).fetchone()
        return _row_to_document(row) if row else None

    def get_content(self, document_id: str) -> Optional[bytes]:
        row = self._conn.execute(
            "SELECT data FROM contents WHERE document_id=?", (document_id,)
        ).fetchone()
        return bytes(row["data"]) if row else None

    def iter_due_documents(
        self,
        status: DocumentStatus,
        due_column: str,
        now: datetime,
        after_id: str = "",
        limit: int = 100,
    ) -> list[Document]:
        """按游标分页拉取到期文档，不一次性加载全部。"""
        assert due_column in ("archive_due_at", "destroy_due_at")
        rows = self._conn.execute(
            f"""SELECT * FROM documents
                WHERE status=? AND {due_column} IS NOT NULL AND {due_column}<=?
                  AND document_id>?
                ORDER BY document_id LIMIT ?""",
            (status.value, now.isoformat(), after_id, limit),
        ).fetchall()
        return [_row_to_document(r) for r in rows]

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
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None:
                raise DocumentNotFoundError(document_id)
            current = DocumentStatus(row["status"])
            if expected is not None and current is not expected:
                raise InvalidStateTransitionError(
                    document_id, current.value, target.value
                )
            if target not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStateTransitionError(
                    document_id, current.value, target.value
                )
            conn.execute(
                """UPDATE documents SET status=?,
                       archived_at=COALESCE(?, archived_at),
                       destroyed_at=COALESCE(?, destroyed_at),
                       content_checksum=COALESCE(?, content_checksum)
                   WHERE document_id=?""",
                (
                    target.value,
                    _dt_to_str(archived_at),
                    _dt_to_str(destroyed_at),
                    checksum,
                    document_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            return _row_to_document(row)

    # ---- 归档数据 ----
    def complete_archive(
        self, document_id: str, data: bytes, checksum: str, archived_at: datetime
    ) -> Document:
        """单事务完成归档：写归档数据、删原文、状态 ARCHIVING -> ARCHIVED。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT status FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None:
                raise DocumentNotFoundError(document_id)
            current = DocumentStatus(row["status"])
            if DocumentStatus.ARCHIVED not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStateTransitionError(
                    document_id, current.value, DocumentStatus.ARCHIVED.value
                )
            conn.execute(
                """INSERT INTO archive_blobs (document_id, data, checksum)
                   VALUES (?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                       data=excluded.data, checksum=excluded.checksum""",
                (document_id, data, checksum),
            )
            conn.execute(
                "DELETE FROM contents WHERE document_id=?", (document_id,)
            )
            conn.execute(
                """UPDATE documents
                   SET status=?, archived_at=?, content_checksum=?
                   WHERE document_id=?""",
                (DocumentStatus.ARCHIVED.value, archived_at.isoformat(),
                 checksum, document_id),
            )
        doc = self.get_document(document_id)
        assert doc is not None
        return doc

    def complete_destroy(
        self, document_id: str, destroyed_at: datetime
    ) -> Document:
        """单事务完成销毁：删归档数据与残留原文、状态 -> DESTROYED。"""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT status FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
            if row is None:
                raise DocumentNotFoundError(document_id)
            current = DocumentStatus(row["status"])
            if DocumentStatus.DESTROYED not in ALLOWED_TRANSITIONS[current]:
                raise InvalidStateTransitionError(
                    document_id, current.value, DocumentStatus.DESTROYED.value
                )
            conn.execute(
                "DELETE FROM archive_blobs WHERE document_id=?", (document_id,)
            )
            conn.execute(
                "DELETE FROM contents WHERE document_id=?", (document_id,)
            )
            conn.execute(
                "UPDATE documents SET status=?, destroyed_at=? WHERE document_id=?",
                (DocumentStatus.DESTROYED.value, destroyed_at.isoformat(),
                 document_id),
            )
        doc = self.get_document(document_id)
        assert doc is not None
        return doc

    def write_archive_blob(
        self, document_id: str, data: bytes, checksum: str
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO archive_blobs (document_id, data, checksum)
                   VALUES (?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                       data=excluded.data, checksum=excluded.checksum""",
                (document_id, data, checksum),
            )

    def read_archive_blob(self, document_id: str) -> tuple[bytes, str]:
        row = self._conn.execute(
            "SELECT data, checksum FROM archive_blobs WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise ArchiveDataMissingError(
                f"archive blob missing for document {document_id!r}"
            )
        return bytes(row["data"]), row["checksum"]

    def delete_content(self, document_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM contents WHERE document_id=?", (document_id,))

    # ---- 策略 ----
    def put_policy(self, policy: RetentionPolicy) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO policies (policy_id, active_days, archived_days, version)
                   VALUES (?,?,?,?)
                   ON CONFLICT(policy_id) DO UPDATE SET
                       active_days=excluded.active_days,
                       archived_days=excluded.archived_days,
                       version=excluded.version""",
                (policy.policy_id, policy.active_days, policy.archived_days,
                 policy.version),
            )

    def get_policy(self, policy_id: str) -> Optional[RetentionPolicy]:
        row = self._conn.execute(
            "SELECT * FROM policies WHERE policy_id=?", (policy_id,)
        ).fetchone()
        if row is None:
            return None
        return RetentionPolicy(
            policy_id=row["policy_id"],
            active_days=row["active_days"],
            archived_days=row["archived_days"],
            version=row["version"],
        )

    def iter_documents_by_policy(
        self, policy_id: str, after_id: str = "", limit: int = 100
    ) -> list[Document]:
        rows = self._conn.execute(
            """SELECT * FROM documents
               WHERE policy_id=? AND document_id>?
               ORDER BY document_id LIMIT ?""",
            (policy_id, after_id, limit),
        ).fetchall()
        return [_row_to_document(r) for r in rows]

    def update_document_schedule(
        self,
        document_id: str,
        archive_due_at: Optional[datetime],
        destroy_due_at: Optional[datetime],
        policy_version: int,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE documents
                   SET archive_due_at=?, destroy_due_at=?, policy_version=?
                   WHERE document_id=?""",
                (
                    _dt_to_str(archive_due_at),
                    _dt_to_str(destroy_due_at),
                    policy_version,
                    document_id,
                ),
            )

    # ---- 法务保全 ----
    def place_hold(self, hold: LegalHold) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO legal_holds
                   (hold_id, document_id, reason, placed_at, released_at)
                   VALUES (?,?,?,?,?)""",
                (
                    hold.hold_id, hold.document_id, hold.reason,
                    hold.placed_at.isoformat(), _dt_to_str(hold.released_at),
                ),
            )

    def release_hold(self, hold_id: str, released_at: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE legal_holds SET released_at=? WHERE hold_id=?",
                (released_at.isoformat(), hold_id),
            )

    def active_holds(self, document_id: str) -> list[LegalHold]:
        rows = self._conn.execute(
            "SELECT * FROM legal_holds WHERE document_id=? AND released_at IS NULL",
            (document_id,),
        ).fetchall()
        return [_row_to_hold(r) for r in rows]

    # ---- 检查点（重启恢复） ----
    def save_checkpoint(self, task_name: str, cursor: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO checkpoints (task_name, cursor, updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(task_name) DO UPDATE SET
                       cursor=excluded.cursor, updated_at=excluded.updated_at""",
                (task_name, cursor, datetime.now().isoformat()),
            )

    def load_checkpoint(self, task_name: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT cursor FROM checkpoints WHERE task_name=?", (task_name,)
        ).fetchone()
        return row["cursor"] if row else None
