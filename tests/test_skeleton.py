"""骨架冒烟测试：确认包可导入、类型可用。"""

from archive_engine import DocumentStatus, RetentionPolicy
from archive_engine.service import DocumentArchiveService


def test_import_and_construct():
    svc = DocumentArchiveService()
    svc.close()


def test_status_enum():
    assert DocumentStatus.ACTIVE.value == "active"
    assert DocumentStatus.DESTROYED.value == "destroyed"


def test_policy_defaults():
    p = RetentionPolicy(policy_id="p1", active_days=30, archived_days=90)
    assert p.version == 1
