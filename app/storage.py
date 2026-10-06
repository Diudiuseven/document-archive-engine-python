"""SQLite 持久化层。

一致性保证：
- 所有多步写操作都在单事务内提交，读取方只会看到完整提交后的状态；
- 状态流转使用条件 UPDATE（WHERE status=期望值），并发下只有一个执行者能成功；
- 处理结果写入 append-only 的 results 表，历史结果不可被改写。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import List, Optional

from .models import (Document, DocumentStatus, FailureReason, ProcessResult,
                     RetentionPolicy, can_transition)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    policy_version INTEGER NOT NULL DEFAULT 1,
    archive_id TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_status_expires
    ON documents(status, expires_at, doc_id);

CREATE TABLE IF NOT EXISTS contents (
    doc_id TEXT PRIMARY KEY,
    content BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    retain_days INTEGER NOT NULL,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS holds (
    doc_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

-- append-only：已提交的处理结果只允许追加，不允许更新或删除。
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    action TEXT NOT NULL,
    ok INTEGER NOT NULL,
    reason TEXT,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


class DocumentStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- 文档基本存取 ---

    def create_document(self, doc: Document, content: bytes) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO documents(doc_id, policy_id, status, created_at,"
                " expires_at, policy_version, archive_id, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (doc.doc_id, doc.policy_id, doc.status.value, doc.created_at,
                 doc.expires_at, doc.policy_version, doc.archive_id,
                 doc.updated_at or time.time()))
            self._conn.execute(
                "INSERT INTO contents(doc_id, content) VALUES(?,?)",
                (doc.doc_id, content))

    def _row_to_doc(self, row: sqlite3.Row) -> Document:
        return Document(
            doc_id=row["doc_id"], policy_id=row["policy_id"],
            status=DocumentStatus(row["status"]),
            created_at=row["created_at"], expires_at=row["expires_at"],
            policy_version=row["policy_version"],
            archive_id=row["archive_id"], updated_at=row["updated_at"])

    def get_document(self, doc_id: str) -> Optional[Document]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        return self._row_to_doc(row) if row else None

    def get_content(self, doc_id: str) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM contents WHERE doc_id=?",
                (doc_id,)).fetchone()
        if row is None:
            raise KeyError(doc_id)
        return bytes(row["content"])

    def delete_content(self, doc_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM contents WHERE doc_id=?",
                               (doc_id,))

    # --- 策略 ---

    def upsert_policy(self, policy: RetentionPolicy) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO policies(policy_id, retain_days, version)"
                " VALUES(?,?,?)"
                " ON CONFLICT(policy_id) DO UPDATE SET"
                " retain_days=excluded.retain_days, version=excluded.version",
                (policy.policy_id, policy.retain_days, policy.version))

    def get_policy(self, policy_id: str) -> Optional[RetentionPolicy]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM policies WHERE policy_id=?",
                (policy_id,)).fetchone()
        if row is None:
            return None
        return RetentionPolicy(policy_id=row["policy_id"],
                               retain_days=row["retain_days"],
                               version=row["version"])

    # --- 状态流转（原子，条件更新） ---

    def transition(self, doc_id: str, expect: DocumentStatus,
                   to: DocumentStatus, **fields) -> bool:
        """仅当当前状态等于 expect 且流转合法时生效；返回是否成功。"""
        if not can_transition(expect, to):
            return False
        sets = ["status=?", "updated_at=?"]
        params: list = [to.value, time.time()]
        for key, value in fields.items():
            if key not in ("archive_id", "expires_at", "policy_version"):
                raise ValueError(f"unsupported field: {key}")
            sets.append(f"{key}=?")
            params.append(value)
        params.extend([doc_id, expect.value])
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE documents SET {', '.join(sets)}"
                " WHERE doc_id=? AND status=?", params)
            return cur.rowcount == 1

    # --- 法务保全 ---

    def place_hold(self, doc_id: str, reason: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO holds(doc_id, reason, created_at) VALUES(?,?,?)"
                " ON CONFLICT(doc_id) DO NOTHING",
                (doc_id, reason, time.time()))

    def release_hold(self, doc_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM holds WHERE doc_id=?", (doc_id,))

    def is_held(self, doc_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM holds WHERE doc_id=?", (doc_id,)).fetchone()
        return row is not None

    # --- 游标分页扫描（按 doc_id 推进，避免 OFFSET 全量扫描） ---

    def _scan(self, where: str, params: tuple, batch_size: int,
              cursor: Optional[str]) -> List[Document]:
        sql = "SELECT * FROM documents WHERE " + where
        if cursor is not None:
            sql += " AND doc_id > ?"
            params = params + (cursor,)
        sql += " ORDER BY doc_id LIMIT ?"
        params = params + (batch_size,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_doc(r) for r in rows]

    def scan_due(self, now: float, batch_size: int,
                 cursor: Optional[str] = None) -> List[Document]:
        """到期且仍处于 ACTIVE 的文档（待归档）。"""
        return self._scan("status=? AND expires_at<=?",
                          (DocumentStatus.ACTIVE.value, now),
                          batch_size, cursor)

    def scan_archived(self, batch_size: int,
                      cursor: Optional[str] = None) -> List[Document]:
        """已归档及待销毁（重启续跑）的文档。"""
        return self._scan(
            "status IN (?,?)",
            (DocumentStatus.ARCHIVED.value,
             DocumentStatus.PENDING_DESTRUCTION.value),
            batch_size, cursor)

    def scan_inflight(self, batch_size: int = 1000) -> List[Document]:
        """处于 ARCHIVING 的 in-flight 文档（用于重启恢复）。"""
        return self._scan("status=?", (DocumentStatus.ARCHIVING.value,),
                          batch_size, None)

    # --- 策略重算：仅影响未完结文档，历史结果不动 ---

    def recompute_expiry(self, policy: RetentionPolicy) -> int:
        """按新策略重算该策略下 ACTIVE 文档的到期时间。

        已进入 ARCHIVING/ARCHIVED/PENDING_DESTRUCTION/DESTROYED 的文档
        属于已处理或处理中的历史，不被追溯改写。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE documents SET expires_at=created_at+?, policy_version=?,"
                " updated_at=? WHERE policy_id=? AND status=?",
                (policy.retain_days * 86400.0, policy.version, time.time(),
                 policy.policy_id, DocumentStatus.ACTIVE.value))
            return cur.rowcount

    # --- append-only 处理结果 ---

    def record_result(self, action: str, result: ProcessResult) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO results(doc_id, action, ok, reason, detail,"
                " created_at) VALUES(?,?,?,?,?,?)",
                (result.doc_id, action, int(result.ok),
                 result.reason.value if result.reason else None,
                 result.detail, time.time()))

    def list_results(self, doc_id: Optional[str] = None) -> List[tuple]:
        with self._lock:
            if doc_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM results ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM results WHERE doc_id=? ORDER BY id",
                    (doc_id,)).fetchall()
        return [tuple(r) for r in rows]
