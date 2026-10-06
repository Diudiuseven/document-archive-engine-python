"""文档归档引擎 HTTP API。

生命周期语义：
  active -> archiving -> archived -> pending_destruction -> destroyed
读取方在任意时刻只能看到上述某个完整已提交状态。
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.archive_store import ArchiveStore
from app.engine import ArchiveEngine
from app.holds import LegalHoldService
from app.models import (ContentCorruptError, ContentMissingError, Document,
                        DocumentStatus, RetentionPolicy)
from app.policy import PolicyService
from app.storage import DocumentStore

DB_PATH = os.environ.get("DAE_DB_PATH", "dae.db")
ARCHIVE_ROOT = os.environ.get("DAE_ARCHIVE_ROOT", "archive_data")
BATCH_SIZE = int(os.environ.get("DAE_BATCH_SIZE", "100"))
WORKERS = int(os.environ.get("DAE_WORKERS", "4"))
DOC_TIMEOUT = float(os.environ.get("DAE_DOC_TIMEOUT", "30"))

store = DocumentStore(DB_PATH)
archive_store = ArchiveStore(ARCHIVE_ROOT)
holds = LegalHoldService(store)
policies = PolicyService(store)
engine = ArchiveEngine(store, archive_store, holds,
                       batch_size=BATCH_SIZE, workers=WORKERS,
                       doc_timeout=DOC_TIMEOUT)
# 启动时回收上次进程遗留的 in-flight 文档（可恢复性）
engine.recover_inflight()

app = FastAPI(title="document-archive-engine")


class PolicyIn(BaseModel):
    policy_id: str
    retain_days: int
    version: int


class DocumentIn(BaseModel):
    doc_id: str
    policy_id: str
    content: str


@app.put("/policies")
def put_policy(p: PolicyIn):
    try:
        recomputed = policies.set_policy(
            RetentionPolicy(p.policy_id, p.retain_days, p.version))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"recomputed": recomputed}


@app.post("/documents", status_code=201)
def create_document(d: DocumentIn):
    policy = policies.get_policy(d.policy_id)
    if policy is None:
        raise HTTPException(404, f"unknown policy {d.policy_id}")
    now = time.time()
    doc = Document(d.doc_id, d.policy_id, DocumentStatus.ACTIVE,
                   created_at=now, expires_at=now + policy.retain_days * 86400,
                   policy_version=policy.version)
    store.create_document(doc, d.content.encode())
    return {"doc_id": d.doc_id, "expires_at": doc.expires_at}


@app.get("/documents/{doc_id}")
def get_document(doc_id: str):
    doc = store.get_document(doc_id)
    if doc is None:
        raise HTTPException(404, "not found")
    return {"doc_id": doc.doc_id, "status": doc.status.value,
            "expires_at": doc.expires_at, "policy_version": doc.policy_version,
            "archive_id": doc.archive_id}


@app.get("/documents/{doc_id}/content")
def get_content(doc_id: str):
    doc = store.get_document(doc_id)
    if doc is None:
        raise HTTPException(404, "not found")
    if doc.status in (DocumentStatus.ARCHIVED,
                      DocumentStatus.PENDING_DESTRUCTION):
        try:
            content, meta = archive_store.read_verified(doc.archive_id or doc_id)
        except ContentMissingError:
            raise HTTPException(410, "archive content missing")
        except ContentCorruptError:
            raise HTTPException(422, "archive content corrupt")
        return {"doc_id": doc_id, "content": content.decode(),
                "sha256": meta["sha256"]}
    if doc.status == DocumentStatus.DESTROYED:
        raise HTTPException(410, "document destroyed")
    try:
        return {"doc_id": doc_id, "content": store.get_content(doc_id).decode()}
    except KeyError:
        raise HTTPException(410, "content missing")


@app.post("/holds/{doc_id}", status_code=204)
def place_hold(doc_id: str, reason: str = ""):
    holds.place_hold(doc_id, reason)


@app.delete("/holds/{doc_id}", status_code=204)
def release_hold(doc_id: str):
    holds.release_hold(doc_id)


@app.post("/engine/archive")
def run_archive():
    results = engine.run_archive_pass()
    return {"processed": len(results),
            "failed": [r.doc_id for r in results if not r.ok]}


@app.post("/engine/destroy")
def run_destroy():
    results = engine.run_destroy_pass()
    return {"processed": len(results),
            "failed": [r.doc_id for r in results if not r.ok]}


@app.post("/engine/cancel", status_code=204)
def cancel():
    engine.cancel()


@app.post("/engine/recover")
def recover():
    return {"recovered": engine.recover_inflight()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=0)
