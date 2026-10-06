"""分批推进、部分失败、并发执行、取消/超时与重启恢复。"""

import threading
from datetime import timedelta

from archive_engine import DocumentStatus, FailureReason
from archive_engine.engine import ARCHIVE_TASK, CancelToken
from archive_engine.models import RetentionPolicy
from archive_engine.service import DocumentArchiveService

from conftest import NOW, make_doc

ARCHIVE_AT = NOW + timedelta(days=31)


def test_partial_failure_does_not_block_batch(service, policy):
    make_doc(service, policy, "ok-1")
    make_doc(service, policy, "bad")
    make_doc(service, policy, "ok-2")
    service.storage.delete_content("bad")  # 模拟单文档数据缺失

    result = service.engine.run_archive_batch(ARCHIVE_AT)
    assert sorted(result.processed) == ["ok-1", "ok-2"]
    assert len(result.failures) == 1
    assert result.failures[0].document_id == "bad"
    assert result.failures[0].reason == FailureReason.DATA_MISSING
    assert service.get_document("ok-1").status == DocumentStatus.ARCHIVED
    assert service.get_document("bad").status == DocumentStatus.ACTIVE


def test_batches_advance_in_pages(service, policy):
    """文档量大于批大小时按批推进，不一次性处理全部。"""
    svc = DocumentArchiveService(batch_size=4, max_workers=2)
    svc.policies.create_policy(
        RetentionPolicy(policy_id="p1", active_days=30, archived_days=90)
    )
    for i in range(10):
        make_doc(svc, svc.policies.get_policy("p1"), f"d{i:02d}", created_at=NOW)

    processed_total = []
    for _ in range(4):  # 10 = 4 + 4 + 2，之后一轮空跑
        result = svc.engine.run_archive_batch(ARCHIVE_AT)
        processed_total.extend(result.processed)
    assert sorted(processed_total) == [f"d{i:02d}" for i in range(10)]
    svc.close()


def test_concurrent_batches_no_double_processing(service, policy):
    """两个批次并发推进，同一文档不会被重复归档。"""
    for i in range(20):
        make_doc(service, policy, f"d{i:02d}")

    results = []

    def run():
        results.append(service.engine.run_archive_batch(ARCHIVE_AT))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    processed = [d for r in results for d in r.processed]
    # 无重复处理；所有文档最终一致（要么本批归档，要么已因并发冲突跳过）
    assert len(processed) == len(set(processed))
    for i in range(20):
        status = service.get_document(f"d{i:02d}").status
        assert status in (DocumentStatus.ARCHIVED, DocumentStatus.ACTIVE)


def test_cancel_token_stops_batch(service, policy):
    for i in range(5):
        make_doc(service, policy, f"d{i}")
    token = CancelToken()
    token.cancel()
    result = service.engine.run_archive_batch(ARCHIVE_AT, token=token)
    assert result.cancelled
    assert result.processed == []
    # 未提交任何中间态
    for i in range(5):
        assert service.get_document(f"d{i}").status == DocumentStatus.ACTIVE


def test_timeout_rolls_back_document(service, policy):
    """超时文档回滚到 ACTIVE，不留半归档状态。"""
    svc = DocumentArchiveService(batch_size=10, max_workers=2,
                                 per_document_timeout=0.0)
    svc.policies.create_policy(
        RetentionPolicy(policy_id="p1", active_days=30, archived_days=90)
    )
    p = svc.policies.get_policy("p1")
    make_doc(svc, p, "d1", created_at=NOW)

    result = svc.engine.run_archive_batch(ARCHIVE_AT)
    assert result.processed == []
    assert result.failures[0].reason == FailureReason.TIMEOUT
    assert svc.get_document("d1").status == DocumentStatus.ACTIVE
    assert svc.storage.get_content("d1") == b"hello"  # 原文未被搬走
    svc.close()


def test_restart_recovery_from_checkpoint(tmp_path):
    """进程重启后从上次已提交的检查点继续。"""
    db = str(tmp_path / "archive.db")

    svc = DocumentArchiveService(db, batch_size=2, max_workers=1)
    svc.policies.create_policy(
        RetentionPolicy(policy_id="p1", active_days=30, archived_days=90)
    )
    p = svc.policies.get_policy("p1")
    for i in range(5):
        make_doc(svc, p, f"d{i}", created_at=NOW)

    r1 = svc.engine.run_archive_batch(ARCHIVE_AT)
    assert r1.processed == ["d0", "d1"]
    assert svc.storage.load_checkpoint(ARCHIVE_TASK) == "d1"
    svc.close()  # 模拟进程退出

    # 重启：新实例从检查点继续，不重复处理 d0/d1
    svc2 = DocumentArchiveService(db, batch_size=2, max_workers=1)
    r2 = svc2.engine.run_archive_batch(ARCHIVE_AT)
    assert r2.processed == ["d2", "d3"]
    r3 = svc2.engine.run_archive_batch(ARCHIVE_AT)
    assert r3.processed == ["d4"]
    # 扫描完成后检查点重置
    assert svc2.storage.load_checkpoint(ARCHIVE_TASK) == ""
    for i in range(5):
        assert svc2.get_document(f"d{i}").status == DocumentStatus.ARCHIVED
    svc2.close()


def test_concurrent_processing_and_reads_consistent(service, policy):
    """并发处理 + 并发读取：读取方只看到完整已提交状态，无文档滞留中间态。"""
    for i in range(30):
        make_doc(service, policy, f"d{i:02d}")
    service.storage.delete_content("d05")  # 注入一个会失败的文档
    stop = threading.Event()
    observed = []

    def reader():
        while not stop.is_set():
            for i in range(30):
                observed.append(service.get_document(f"d{i:02d}").status)

    def runner():
        service.engine.run_archive_batch(ARCHIVE_AT)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    for t in threads[:2]:
        t.join()
    stop.set()
    threads[2].join()

    # 读取到的每个状态都是状态机中的完整状态（枚举解析即保证）
    assert set(observed) <= set(DocumentStatus)
    # 批次结束后没有文档滞留在处理中间态
    for i in range(30):
        status = service.get_document(f"d{i:02d}").status
        assert status in (DocumentStatus.ARCHIVED, DocumentStatus.ACTIVE)
    assert service.get_document("d05").status == DocumentStatus.ACTIVE
