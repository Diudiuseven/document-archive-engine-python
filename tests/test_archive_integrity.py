"""归档内容校验：底层数据缺失或损坏时给出可区分错误，不静默返回空结果。"""

from datetime import timedelta

import pytest

from archive_engine import (
    ArchiveDataCorruptedError,
    ArchiveDataMissingError,
    DocumentStatus,
)

from conftest import NOW, make_doc

ARCHIVE_AT = NOW + timedelta(days=31)


def _archived(service, policy, doc_id, content=b"payload"):
    make_doc(service, policy, doc_id, content=content)
    service.engine.run_archive_batch(ARCHIVE_AT)
    assert service.get_document(doc_id).status == DocumentStatus.ARCHIVED


def test_read_archived_roundtrip(service, policy):
    _archived(service, policy, "d1", content=b"\x00\x01binary\xff")
    assert service.read_archived_content("d1") == b"\x00\x01binary\xff"


def test_missing_blob_raises_distinguishable_error(service, policy):
    _archived(service, policy, "d1")
    # 模拟底层归档数据丢失
    service.storage._conn.execute(
        "DELETE FROM archive_blobs WHERE document_id='d1'"
    )
    service.storage._conn.commit()
    with pytest.raises(ArchiveDataMissingError):
        service.read_archived_content("d1")


def test_corrupted_blob_raises_distinguishable_error(service, policy):
    _archived(service, policy, "d1", content=b"original")
    # 模拟底层数据被篡改（内容变了、校验和未同步）
    service.storage._conn.execute(
        "UPDATE archive_blobs SET data=? WHERE document_id='d1'",
        (b"tampered",),
    )
    service.storage._conn.commit()
    with pytest.raises(ArchiveDataCorruptedError):
        service.read_archived_content("d1")


def test_checksum_mismatch_against_metadata(service, policy):
    _archived(service, policy, "d1", content=b"original")
    # 归档数据整体被替换（自带一致校验和，但与元数据记录不符）
    import hashlib

    fake = b"replaced-content"
    service.storage.write_archive_blob("d1", fake, hashlib.sha256(fake).hexdigest())
    with pytest.raises(ArchiveDataCorruptedError):
        service.read_archived_content("d1")


def test_destroyed_document_has_no_archive_data(service, policy):
    _archived(service, policy, "d1")
    service.engine.run_destroy_batch(NOW + timedelta(days=200))
    assert service.get_document("d1").status == DocumentStatus.DESTROYED
    with pytest.raises(ArchiveDataMissingError):
        service.storage.read_archive_blob("d1")
