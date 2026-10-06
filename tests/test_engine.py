"""需求 4/5/6/7：部分失败、错误可区分、分批游标、超时取消、归档校验。"""
import os
import threading
import time

from app.engine import ArchiveEngine
from app.models import (ContentCorruptError, ContentMissingError,
                        DocumentStatus, FailureReason)

import pytest


def test_partial_failure_does_not_block_batch(engine, store, archive,
                                              make_doc):
    make_doc("ok1")
    make_doc("bad")
    make_doc("ok2")
    store.delete_content("bad")  # 制造单文档失败：原文缺失

    results = engine.run_archive_pass()
    by_id = {r.doc_id: r for r in results}

    assert by_id["bad"].reason == FailureReason.CONTENT_MISSING
    assert by_id["ok1"].ok and by_id["ok2"].ok
    # 失败文档回滚到 ACTIVE 可重试，不会卡在中间态
    assert store.get_document("bad").status == DocumentStatus.ACTIVE
    assert store.get_document("ok1").status == DocumentStatus.ARCHIVED


def test_archive_read_distinguishes_missing_and_corrupt(engine, store,
                                                        archive, make_doc):
    make_doc("m")
    make_doc("c")
    engine.run_archive_pass()

    archive.remove("m")
    with pytest.raises(ContentMissingError):
        archive.read_verified("m")

    bin_path = os.path.join(archive._root, "c.bin")
    with open(bin_path, "wb") as f:
        f.write(b"tampered")
    with pytest.raises(ContentCorruptError):
        archive.read_verified("c")

    # 销毁同样拒绝并给出可区分原因，绝不静默
    results = {r.doc_id: r for r in engine.run_destroy_pass()}
    assert results["m"].reason == FailureReason.CONTENT_MISSING
    assert results["c"].reason == FailureReason.CONTENT_CORRUPT
    assert store.get_document("m").status == DocumentStatus.ARCHIVED
    assert store.get_document("c").status == DocumentStatus.ARCHIVED


def test_batched_cursor_pagination(store, archive, holds, make_doc):
    # batch_size=2，5 个文档 => 3 批；验证游标推进不漏不重
    engine = ArchiveEngine(store, archive, holds, batch_size=2, workers=2)
    for i in range(5):
        make_doc(f"doc{i}")
    results = engine.run_archive_pass()
    assert len(results) == 5
    assert len({r.doc_id for r in results}) == 5
    assert all(r.ok for r in results)


def test_timeout_rolls_back(store, archive, holds, make_doc):
    engine = ArchiveEngine(store, archive, holds, doc_timeout=0.0)
    make_doc("t1")
    results = engine.run_archive_pass()
    assert results[0].reason == FailureReason.TIMEOUT
    # 超时后文档回到 ACTIVE，不留半归档状态
    assert store.get_document("t1").status == DocumentStatus.ACTIVE


def test_cancel_rolls_back_and_stops(store, archive, holds, make_doc):
    engine = ArchiveEngine(store, archive, holds, batch_size=10, workers=1)
    for i in range(4):
        make_doc(f"c{i}")
    engine.cancel()  # 在认领前即取消
    results = engine.run_archive_pass()
    assert results == []
    for i in range(4):
        assert store.get_document(f"c{i}").status == DocumentStatus.ACTIVE
    engine.reset_cancel()


def test_concurrent_readers_see_committed_states(engine, store, make_doc):
    for i in range(20):
        make_doc(f"r{i}")
    valid = {s.value for s in DocumentStatus}
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            for i in range(20):
                doc = store.get_document(f"r{i}")
                if doc.status.value not in valid:
                    errors.append(doc.status)
                # 已归档的文档必须带有 archive_id（状态与字段同事务提交）
                if doc.status == DocumentStatus.ARCHIVED and not doc.archive_id:
                    errors.append("partial state")

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    engine.run_archive_pass()
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
