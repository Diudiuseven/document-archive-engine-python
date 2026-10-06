"""策略变更：到期时间重算，历史结果不被追溯改写。"""

from datetime import timedelta

from archive_engine import DocumentStatus, RetentionPolicy

from conftest import NOW, make_doc


def test_policy_update_recomputes_due_times(service, policy):
    doc = make_doc(service, policy, "d1")
    assert doc.archive_due_at == NOW + timedelta(days=30)
    assert doc.destroy_due_at == NOW + timedelta(days=120)
    assert doc.policy_version == 1

    updated = service.policies.update_policy("p1", active_days=10, archived_days=20)
    assert updated.version == 2

    doc = service.get_document("d1")
    assert doc.archive_due_at == NOW + timedelta(days=10)
    assert doc.destroy_due_at == NOW + timedelta(days=30)
    assert doc.policy_version == 2


def test_policy_update_shortens_then_batch_picks_up(service, policy):
    make_doc(service, policy, "d1")
    # 按旧策略未到期
    result = service.engine.run_archive_batch(NOW + timedelta(days=15))
    assert result.succeeded == 0
    # 缩短保留期后，同一时刻到期
    service.policies.update_policy("p1", active_days=10)
    result = service.engine.run_archive_batch(NOW + timedelta(days=15))
    assert result.processed == ["d1"]


def test_history_not_rewritten(service, policy):
    """已归档时间、已销毁记录不被策略变更改写。"""
    make_doc(service, policy, "archived-doc")
    make_doc(service, policy, "destroyed-doc")
    archive_time = NOW + timedelta(days=31)
    service.engine.run_archive_batch(archive_time)
    archived_before = service.get_document("archived-doc")

    # 销毁一个文档（进入终态）
    service.engine.run_destroy_batch(NOW + timedelta(days=200))
    destroyed_before = service.get_document("destroyed-doc")
    assert destroyed_before.status == DocumentStatus.DESTROYED
    destroy_due_before = destroyed_before.destroy_due_at

    # 变更策略
    service.policies.update_policy("p1", active_days=5, archived_days=5)

    archived_after = service.get_document("archived-doc")
    destroyed_after = service.get_document("destroyed-doc")
    # 归档完成时间（历史结果）不变
    assert archived_after.archived_at == archived_before.archived_at
    # 已销毁文档的调度不再被重算
    assert destroyed_after.destroy_due_at == destroy_due_before
    assert destroyed_after.destroyed_at == destroyed_before.destroyed_at
    assert destroyed_after.policy_version == 1


def test_recompute_paginates_large_sets(service, policy):
    """大量文档时重算按分页推进，结果仍然完整。"""
    for i in range(250):
        make_doc(service, policy, f"d{i:04d}")
    service.policies.update_policy("p1", active_days=7)
    doc_first = service.get_document("d0000")
    doc_last = service.get_document("d0249")
    expected = NOW + timedelta(days=7)
    assert doc_first.archive_due_at == expected
    assert doc_last.archive_due_at == expected
    assert doc_last.policy_version == 2
