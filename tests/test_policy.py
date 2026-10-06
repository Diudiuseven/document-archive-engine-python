"""需求 1：策略变更后重算到期时间，历史结果不被追溯改写。"""
import time

from app.models import DocumentStatus, RetentionPolicy


def test_policy_change_recomputes_active_docs(store, policies, make_doc):
    make_doc("a", expired=False)
    make_doc("b", expired=False)
    created = store.get_document("a").created_at
    n = policies.set_policy(RetentionPolicy("p1", 7, 2))
    assert n == 2
    after = store.get_document("a").expires_at
    # 按新策略基于原创建时间重算，不随重算时刻漂移
    assert abs(after - (created + 7 * 86400)) < 1.0
    assert store.get_document("a").policy_version == 2


def test_policy_change_does_not_rewrite_processed(store, policies, make_doc,
                                                  engine):
    make_doc("done")
    make_doc("open", expired=False)
    engine.run_archive_pass()
    assert store.get_document("done").status == DocumentStatus.ARCHIVED
    results_before = store.list_results("done")

    policies.set_policy(RetentionPolicy("p1", 1, 2))

    done = store.get_document("done")
    assert done.status == DocumentStatus.ARCHIVED
    assert done.policy_version == 1  # 已归档文档不被追溯
    assert store.list_results("done") == results_before  # 历史结果不变
    assert store.get_document("open").policy_version == 2


def test_policy_version_must_increase(policies):
    policies.set_policy(RetentionPolicy("px", 10, 3))
    import pytest
    with pytest.raises(ValueError):
        policies.set_policy(RetentionPolicy("px", 10, 3))
