"""
HITL Dashboard (FastAPI)
------------------------
REST API that exposes the ApprovalQueue to human operators.
Operators can:
  GET  /queue          – list all pending requests
  GET  /queue/{id}     – get a single request
  POST /queue/{id}/approve  – approve (optionally partial)
  POST /queue/{id}/reject   – reject

Run standalone:
    uvicorn hitl.dashboard:app --reload --port 8080
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agents.risk_agent import RecommendedAction
from hitl.approval_queue import ApprovalQueue, ApprovalRequest

app = FastAPI(
    title="Pharma Cargo Monitor – HITL Dashboard",
    description="Human-in-the-loop approval interface for AI-recommended actions.",
    version="1.0.0",
)

# Shared queue instance — injected by main.py at startup
_queue: Optional[ApprovalQueue] = None


def set_queue(queue: ApprovalQueue) -> None:
    global _queue
    _queue = queue


def _get_queue() -> ApprovalQueue:
    if _queue is None:
        raise RuntimeError("ApprovalQueue not initialised — call set_queue() at startup.")
    return _queue


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ApproveRequest(BaseModel):
    operator:         str
    approved_actions: Optional[List[str]] = None   # None = approve all
    notes:            str = ""


class RejectRequest(BaseModel):
    operator: str
    notes:    str = ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/queue", response_model=List[dict], tags=["HITL"])
def list_pending():
    """Return all pending approval requests."""
    return [r.to_dict() for r in _get_queue().pending()]


@app.get("/queue/all", response_model=List[dict], tags=["HITL"])
def list_all():
    """Return all requests (any status)."""
    return [r.to_dict() for r in _get_queue().all_requests()]


@app.get("/queue/{request_id}", response_model=dict, tags=["HITL"])
def get_request(request_id: str):
    req = _get_queue().get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return req.to_dict()


@app.post("/queue/{request_id}/approve", response_model=dict, tags=["HITL"])
def approve_request(request_id: str, body: ApproveRequest):
    queue = _get_queue()
    req   = queue.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")

    actions = None
    if body.approved_actions is not None:
        try:
            actions = [RecommendedAction(a) for a in body.approved_actions]
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    updated = queue.approve(request_id, body.operator, actions, body.notes)
    return updated.to_dict()


@app.post("/queue/{request_id}/reject", response_model=dict, tags=["HITL"])
def reject_request(request_id: str, body: RejectRequest):
    queue = _get_queue()
    req   = queue.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    updated = queue.reject(request_id, body.operator, body.notes)
    return updated.to_dict()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "service": "pharma-cargo-hitl-dashboard"}
