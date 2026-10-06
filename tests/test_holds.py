"""需求 3：法务保全排除自动销毁，解除后重新进入原进度。"""
from app.models import DocumentStatus, FailureReason


def _archived(engine, store, make_doc, doc_id):
    make_doc(doc_id)
    engine.run_archive_pass()
    assert store.get_document(doc_id).status == DocumentStatus.ARCHIVED


def test_hold_blocks_destroy(engine, store, holds, make_doc):
    _archived(engine, store, make_doc, "h1")
    holds.place_hold("h1", "litigation")

    results = engine.run_destroy_pass()

    assert len(results) == 1
    assert not results[0].ok
    assert results[0].reason == FailureReason.LEGAL_HOLD
    # 状态保持 ARCHIVED，不被销毁也不跳过
    assert store.get_document("h1").status == DocumentStatus.ARCHIVED


def test_release_hold_resumes_progress(engine, store, holds, make_doc):
    _archived(engine, store, make_doc, "h2")
    holds.place_hold("h2")
    engine.run_destroy_pass()
    holds.release_hold("h2")

    results = engine.run_destroy_pass()

    assert results[0].ok
    assert store.get_document("h2").status == DocumentStatus.DESTROYED
    # 整个生命周期恰好归档一次、销毁一次，无重复处理
    actions = [r[2] for r in store.list_results("h2") if r[3] == 1]
    assert actions.count("archive") == 1
    assert actions.count("destroy") == 1


def test_hold_does_not_block_other_docs(engine, store, holds, make_doc):
    _archived(engine, store, make_doc, "held")
    _archived(engine, store, make_doc, "free")
    holds.place_hold("held")

    engine.run_destroy_pass()

    assert store.get_document("held").status == DocumentStatus.ARCHIVED
    assert store.get_document("free").status == DocumentStatus.DESTROYED
