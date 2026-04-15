"""
HITL Dashboard (FastAPI)
------------------------
REST API + visual HTML dashboard for human operators.

Endpoints:
  GET  /                        - visual HTML dashboard (operator screen)
  GET  /queue                   - pending approval requests (JSON)
  GET  /queue/all               - all requests any status (JSON)
  GET  /queue/{id}              - single request (JSON)
  POST /queue/{id}/approve      - approve (optionally partial)
  POST /queue/{id}/reject       - reject
  GET  /audit?n=50              - last N audit log records (JSON)
  GET  /health                  - health check

Run:
    python main.py dashboard
    # then open http://localhost:8080
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agents.risk_agent import RecommendedAction
from hitl.approval_queue import ApprovalQueue, ApprovalRequest
from hitl._dashboard_html import get_dashboard_html
from config import AUDIT_LOG_PATH

app = FastAPI(
    title="Pharma Cargo Monitor - HITL Dashboard",
    description="Human-in-the-loop approval interface for AI-recommended actions.",
    version="1.0.0",
)

# Shared queue instance - injected by main.py at startup
_queue: Optional[ApprovalQueue] = None


def set_queue(queue: ApprovalQueue) -> None:
    global _queue
    _queue = queue


def _get_queue() -> ApprovalQueue:
    if _queue is None:
        raise RuntimeError("ApprovalQueue not initialised - call set_queue() at startup.")
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
# HTML Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    """Serve the visual HITL operator dashboard."""
    return HTMLResponse(content=get_dashboard_html())


# ---------------------------------------------------------------------------
# Queue endpoints
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
# Audit log endpoint
# ---------------------------------------------------------------------------

@app.get("/audit", response_model=List[dict], tags=["Compliance"])
def get_audit_log(
    n: int = Query(default=50, ge=1, le=500, description="Number of most-recent records to return"),
    event_type: Optional[str] = Query(default=None, description="Filter by event_type"),
):
    """
    Return the last N records from the ALCOA+ audit log.
    Optionally filter by event_type (PIPELINE_RUN, RISK_ASSESSMENT, HITL_DECISION, etc.).
    """
    path = Path(AUDIT_LOG_PATH)
    records = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if event_type is None or rec.get("event_type") == event_type:
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
    # Return last N in reverse-chronological order
    return list(reversed(records[-n:]))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health():
    """Health check."""
    q = _get_queue()
    return {
        "status": "ok",
        "pending_count": len(q.pending()),
        "total_count":   len(q.all_requests()),
    }
