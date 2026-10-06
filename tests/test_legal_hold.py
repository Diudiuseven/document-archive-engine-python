"""法务保全：命中保全的文档排除在自动销毁之外，解除后恢复进度。"""

from datetime import timedelta

from archive_engine import DocumentStatus, FailureReason

from conftest import NOW, make_doc

ARCHIVE_AT = NOW + timedelta(days=31)
DESTROY_AT = NOW + timedelta(days=200)


def _archived_doc(service, policy, doc_id):
    make_doc(service, policy, doc_id)
    service.engine.run_archive_batch(ARCHIVE_AT)
    assert service.get_document(doc_id).status == DocumentStatus.ARCHIVED


def test_hold_blocks_destroy_and_rolls_back(service, policy):
    _archived_doc(service, policy, "d1")
    hold = service.place_hold("d1", "litigation-123")

    result = service.engine.run_destroy_batch(DESTROY_AT)
    assert result.succeeded == 0
    assert len(result.failures) == 1
    assert result.failures[0].reason == FailureReason.LEGAL_HOLD

    # 文档回退到 ARCHIVED，不停留在 PENDING_DESTRUCTION 中间态
    doc = service.get_document("d1")
    assert doc.status == DocumentStatus.ARCHIVED
    # 归档内容仍可读取校验
    assert service.read_archived_content("d1") == b"hello"

    # 解除保全后重新进入销毁进度，不跳过
    service.release_hold(hold.hold_id)
    result = service.engine.run_destroy_batch(DESTROY_AT, resume=False)
    assert result.processed == ["d1"]
    assert service.get_document("d1").status == DocumentStatus.DESTROYED


def test_hold_does_not_affect_other_documents(service, policy):
    _archived_doc(service, policy, "held")
    _archived_doc(service, policy, "free")
    service.place_hold("held", "investigation")

    result = service.engine.run_destroy_batch(DESTROY_AT)
    assert result.processed == ["free"]
    assert [f.document_id for f in result.failures] == ["held"]
    assert service.get_document("free").status == DocumentStatus.DESTROYED
    assert service.get_document("held").status == DocumentStatus.ARCHIVED


def test_hold_not_double_processed_after_release(service, policy):
    """解除保全后再次推进，文档只被销毁一次，后续批次不再重复处理。"""
    _archived_doc(service, policy, "d1")
    hold = service.place_hold("d1", "audit")
    service.engine.run_destroy_batch(DESTROY_AT)
    service.release_hold(hold.hold_id)

    result = service.engine.run_destroy_batch(DESTROY_AT, resume=False)
    assert result.processed == ["d1"]
    # 再次推进：已销毁文档不再出现
    result = service.engine.run_destroy_batch(DESTROY_AT, resume=False)
    assert result.processed == []
    assert result.failures == []
