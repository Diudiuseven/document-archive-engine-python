import time

import pytest

from app.archive_store import ArchiveStore
from app.engine import ArchiveEngine
from app.holds import LegalHoldService
from app.models import Document, DocumentStatus, RetentionPolicy
from app.policy import PolicyService
from app.storage import DocumentStore


@pytest.fixture()
def store():
    s = DocumentStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def archive(tmp_path):
    return ArchiveStore(str(tmp_path / "archive"))


@pytest.fixture()
def holds(store):
    return LegalHoldService(store)


@pytest.fixture()
def policies(store):
    return PolicyService(store)


@pytest.fixture()
def engine(store, archive, holds):
    return ArchiveEngine(store, archive, holds, batch_size=3, workers=4)


@pytest.fixture()
def make_doc(store, policies):
    policies.set_policy(RetentionPolicy("p1", 30, 1))

    def _make(doc_id, *, expired=True, policy_id="p1", content=None):
        now = time.time()
        expires = now - 1 if expired else now + 86400
        store.create_document(
            Document(doc_id, policy_id, DocumentStatus.ACTIVE,
                     created_at=now, expires_at=expires),
            content if content is not None else f"c-{doc_id}".encode())
        return doc_id

    return _make
