"""需求 4：进程重启后从上次已提交位置继续。"""
import os

from app.archive_store import ArchiveStore
from app.engine import ArchiveEngine
from app.holds import LegalHoldService
from app.models import Document, DocumentStatus, RetentionPolicy
from app.storage import DocumentStore


def _build(tmp_path):
    store = DocumentStore(str(tmp_path / "test.db"))
    archive = ArchiveStore(str(tmp_path / "arch"))
    holds = LegalHoldService(store)
    engine = ArchiveEngine(store, archive, holds, batch_size=2, workers=2)
    return store, archive, holds, engine


def test_restart_recovers_inflight(tmp_path):
    store, archive, holds, engine = _build(tmp_path)
    import time
    now = time.time()
    store.upsert_policy(RetentionPolicy("p1", 30, 1))
    for i in range(3):
        store.create_document(
            Document(f"d{i}", "p1", DocumentStatus.ACTIVE, now, now - 1),
            b"data")
    # 模拟崩溃：d0 被认领为 ARCHIVING 但未提交
    store.transition("d0", DocumentStatus.ACTIVE, DocumentStatus.ARCHIVING)
    store.close()

    # “重启”：新实例在同一数据库上恢复
    store2, archive2, holds2, engine2 = _build(tmp_path)
    recovered = engine2.recover_inflight()
    assert recovered == 1
    assert store2.get_document("d0").status == DocumentStatus.ACTIVE

    results = engine2.run_archive_pass()
    assert len(results) == 3 and all(r.ok for r in results)
    for i in range(3):
        assert store2.get_document(f"d{i}").status == DocumentStatus.ARCHIVED
    store2.close()


def test_restart_resumes_pending_destruction(tmp_path):
    store, archive, holds, engine = _build(tmp_path)
    import time
    now = time.time()
    store.upsert_policy(RetentionPolicy("p1", 30, 1))
    store.create_document(
        Document("z", "p1", DocumentStatus.ACTIVE, now, now - 1), b"data")
    engine.run_archive_pass()
    # 模拟崩溃在销毁中途：已进入 PENDING_DESTRUCTION 但未完成
    store.transition("z", DocumentStatus.ARCHIVED,
                     DocumentStatus.PENDING_DESTRUCTION)
    store.close()

    store2, archive2, holds2, engine2 = _build(tmp_path)
    results = engine2.run_destroy_pass()
    assert results[0].ok
    assert store2.get_document("z").status == DocumentStatus.DESTROYED
    store2.close()
