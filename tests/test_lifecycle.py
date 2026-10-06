"""生命周期状态流转与一致性测试。"""

from datetime import timedelta

import pytest

from archive_engine import DocumentStatus, InvalidStateTransitionError
from archive_engine.errors import ArchiveDataMissingError

from conftest import NOW, make_doc


def test_full_lifecycle(service, policy):
    doc = make_doc(service, policy, "d1")
    assert doc.status == DocumentStatus.ACTIVE

    # 到期前不处理
    result = service.engine.run_archive_batch(NOW + timedelta(days=29))
    assert result.succeeded == 0

    # 到期后归档
    result = service.engine.run_archive_batch(NOW + timedelta(days=31))
    assert result.processed == ["d1"]
    doc = service.get_document("d1")
    assert doc.status == DocumentStatus.ARCHIVED
    assert doc.archived_at is not None
    assert doc.content_checksum is not None
    # 原文已搬走
    assert service.storage.get_content("d1") is None

    # 归档保留期未到期不销毁
    result = service.engine.run_destroy_batch(NOW + timedelta(days=31))
    assert result.succeeded == 0

    # 到期销毁
    result = service.engine.run_destroy_batch(NOW + timedelta(days=200))
    assert result.processed == ["d1"]
    doc = service.get_document("d1")
    assert doc.status == DocumentStatus.DESTROYED
    assert doc.destroyed_at is not None


def test_archived_content_readable(service, policy):
    make_doc(service, policy, "d1", content=b"payload-1")
    service.engine.run_archive_batch(NOW + timedelta(days=31))
    assert service.read_archived_content("d1") == b"payload-1"


def test_illegal_transition_rejected(service, policy):
    make_doc(service, policy, "d1")
    with pytest.raises(InvalidStateTransitionError):
        service.storage.transition("d1", DocumentStatus.DESTROYED)


def test_failed_archive_rolls_back_to_consistent_state(service, policy):
    """归档中途失败（原文缺失）不得留下半归档状态。"""
    make_doc(service, policy, "d1")
    service.storage.delete_content("d1")  # 模拟底层数据缺失
    result = service.engine.run_archive_batch(NOW + timedelta(days=31))
    assert result.succeeded == 0
    assert len(result.failures) == 1
    assert result.failures[0].reason.value == "data_missing"
    # 状态回滚到 ACTIVE，且无归档残留
    assert service.get_document("d1").status == DocumentStatus.ACTIVE
    with pytest.raises(ArchiveDataMissingError):
        service.storage.read_archive_blob("d1")
