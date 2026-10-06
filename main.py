"""HTTP API：文档归档生命周期引擎。"""

from __future__ import annotations

import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from archive_engine.errors import (
    ArchiveDataCorruptedError,
    ArchiveDataMissingError,
    ArchiveEngineError,
    DocumentNotFoundError,
    PolicyNotFoundError,
)
from archive_engine.models import RetentionPolicy
from archive_engine.service import DocumentArchiveService

service = DocumentArchiveService(
    os.environ.get("ARCHIVE_DB", ":memory:"),
    batch_size=int(os.environ.get("ARCHIVE_BATCH_SIZE", "50")),
    max_workers=int(os.environ.get("ARCHIVE_MAX_WORKERS", "4")),
    per_document_timeout=float(os.environ.get("ARCHIVE_DOC_TIMEOUT", "30")),
)
app = FastAPI(title="document-archive-engine")


class PolicyIn(BaseModel):
    policy_id: str
    active_days: int
    archived_days: int


class PolicyUpdateIn(BaseModel):
    active_days: Optional[int] = None
    archived_days: Optional[int] = None


class DocumentIn(BaseModel):
    policy_id: str
    content: str
    document_id: Optional[str] = None


class HoldIn(BaseModel):
    reason: str


def _doc_view(doc) -> dict:
    return {
        "document_id": doc.document_id,
        "policy_id": doc.policy_id,
        "status": doc.status.value,
        "created_at": doc.created_at.isoformat(),
        "archive_due_at": doc.archive_due_at.isoformat() if doc.archive_due_at else None,
        "destroy_due_at": doc.destroy_due_at.isoformat() if doc.destroy_due_at else None,
        "policy_version": doc.policy_version,
        "archived_at": doc.archived_at.isoformat() if doc.archived_at else None,
        "destroyed_at": doc.destroyed_at.isoformat() if doc.destroyed_at else None,
    }


@app.exception_handler(DocumentNotFoundError)
@app.exception_handler(PolicyNotFoundError)
async def _not_found(request, exc):
    raise HTTPException(status_code=404, detail=str(exc))


@app.post("/policies", status_code=201)
def create_policy(body: PolicyIn):
    policy = service.policies.create_policy(RetentionPolicy(**body.model_dump()))
    return policy.__dict__


@app.patch("/policies/{policy_id}")
def update_policy(policy_id: str, body: PolicyUpdateIn):
    try:
        policy = service.policies.update_policy(policy_id, **body.model_dump())
    except PolicyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return policy.__dict__


@app.post("/documents", status_code=201)
def create_document(body: DocumentIn):
    try:
        doc = service.create_document(
            body.policy_id, body.content.encode(), body.document_id
        )
    except PolicyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _doc_view(doc)


@app.get("/documents/{document_id}")
def get_document(document_id: str):
    try:
        return _doc_view(service.get_document(document_id))
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/documents/{document_id}/archive")
def read_archive(document_id: str):
    try:
        data = service.read_archived_content(document_id)
        return Response(content=data, media_type="application/octet-stream")
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ArchiveDataMissingError as exc:
        raise HTTPException(status_code=410, detail=str(exc))
    except ArchiveDataCorruptedError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ArchiveEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/documents/{document_id}/holds", status_code=201)
def place_hold(document_id: str, body: HoldIn):
    try:
        hold = service.place_hold(document_id, body.reason)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"hold_id": hold.hold_id, "document_id": hold.document_id}


@app.delete("/holds/{hold_id}", status_code=204)
def release_hold(hold_id: str):
    service.release_hold(hold_id)


@app.post("/tasks/archive")
def run_archive():
    result = service.run_archive_batch()
    return {
        "processed": result.processed,
        "failures": [f.__dict__ for f in result.failures],
        "cancelled": result.cancelled,
    }


@app.post("/tasks/destroy")
def run_destroy():
    result = service.run_destroy_batch()
    return {
        "processed": result.processed,
        "failures": [f.__dict__ for f in result.failures],
        "cancelled": result.cancelled,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
