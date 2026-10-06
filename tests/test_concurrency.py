"""需求 4/5：并发执行下每个文档恰好被处理一次。"""
import threading

from app.engine import ArchiveEngine
from app.models import DocumentStatus


def test_concurrent_archive_passes_process_each_doc_once(store, archive,
                                                         holds, make_doc):
    for i in range(30):
        make_doc(f"d{i}")
    # 两个引擎实例共享同一存储，模拟多执行者并发
    e1 = ArchiveEngine(store, archive, holds, batch_size=4, workers=4)
    e2 = ArchiveEngine(store, archive, holds, batch_size=4, workers=4)

    t1 = threading.Thread(target=e1.run_archive_pass)
    t2 = threading.Thread(target=e2.run_archive_pass)
    t1.start(); t2.start(); t1.join(); t2.join()

    for i in range(30):
        assert store.get_document(f"d{i}").status == DocumentStatus.ARCHIVED
    # 每个文档恰好归档成功一次（条件更新保证认领互斥）
    for i in range(30):
        ok_archives = [r for r in store.list_results(f"d{i}")
                       if r[2] == "archive" and r[3] == 1]
        assert len(ok_archives) == 1


def test_concurrent_destroy_with_hold_flip(store, archive, holds, make_doc):
    make_doc("x")
    engine = ArchiveEngine(store, archive, holds, workers=2)
    engine.run_archive_pass()
    holds.place_hold("x")
    holds.release_hold("x")
    results = engine.run_destroy_pass()
    assert results[0].ok
    assert store.get_document("x").status == DocumentStatus.DESTROYED
