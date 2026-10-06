from datetime import datetime, timedelta, timezone

import pytest

from archive_engine.models import RetentionPolicy
from archive_engine.service import DocumentArchiveService

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def service():
    svc = DocumentArchiveService(batch_size=50, max_workers=4)
    yield svc
    svc.close()


@pytest.fixture
def policy(service):
    return service.policies.create_policy(
        RetentionPolicy(policy_id="p1", active_days=30, archived_days=90)
    )


def make_doc(service, policy, doc_id, content=b"hello", created_at=NOW):
    return service.create_document(
        policy.policy_id, content, document_id=doc_id, created_at=created_at
    )
