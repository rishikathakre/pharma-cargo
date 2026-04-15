"""
ApprovalQueue
-------------
Human-in-the-loop approval gate.  High-risk actions are queued here for
operator review.  Supports:
  - Synchronous wait (with configurable timeout + auto-escalation)
  - Partial approval (operator approves subset of recommended actions)
  - Audit trail for every decision

In production, this module would integrate with a ticketing system,
Slack bot, or web dashboard (see hitl/dashboard.py).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from agents.risk_agent import RecommendedAction, RiskAssessment, RiskLevel
from config import HITL_APPROVAL_TIMEOUT_SEC

logger = logging.getLogger(__name__)


class ApprovalStatus(str, Enum):
    PENDING  = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PARTIAL  = "PARTIAL"
    TIMEOUT  = "TIMEOUT"


@dataclass
class ApprovalRequest:
    request_id:        str
    shipment_id:       str
    risk_level:        RiskLevel
    risk_score:        float
    proposed_actions:  List[RecommendedAction]
    justification:     str
    created_at:        datetime
    status:            ApprovalStatus = ApprovalStatus.PENDING
    approved_actions:  List[RecommendedAction] = field(default_factory=list)
    decided_by:        Optional[str] = None
    decided_at:        Optional[datetime] = None
    notes:             str = ""

    def to_dict(self) -> dict:
        return {
            "request_id":       self.request_id,
            "shipment_id":      self.shipment_id,
            "risk_level":       self.risk_level.value,
            "risk_score":       round(self.risk_score, 4),
            "proposed_actions": [a.value for a in self.proposed_actions],
            "justification":    self.justification,
            "created_at":       self.created_at.isoformat(),
            "status":           self.status.value,
            "approved_actions": [a.value for a in self.approved_actions],
            "decided_by":       self.decided_by,
            "decided_at":       self.decided_at.isoformat() if self.decided_at else None,
            "notes":            self.notes,
        }


class ApprovalQueue:
    """Thread-safe queue for human-in-the-loop approval requests."""

    def __init__(self, timeout_sec: int = HITL_APPROVAL_TIMEOUT_SEC):
        self._queue:   Dict[str, ApprovalRequest] = {}
        self._events:  Dict[str, threading.Event] = {}
        self._lock     = threading.Lock()
        self._timeout  = timeout_sec

    # ------------------------------------------------------------------
    # Submit & wait
    # ------------------------------------------------------------------

    def submit(self, assessment: RiskAssessment) -> ApprovalRequest:
        req = ApprovalRequest(
            request_id       = str(uuid.uuid4()),
            shipment_id      = assessment.shipment_id,
            risk_level       = assessment.risk_level,
            risk_score       = assessment.risk_score,
            proposed_actions = assessment.actions,
            justification    = assessment.justification,
            created_at       = datetime.now(timezone.utc),
        )
        event = threading.Event()
        with self._lock:
            self._queue[req.request_id] = req
            self._events[req.request_id] = event

        logger.info("HITL request %s submitted for shipment %s [%s]",
                    req.request_id, req.shipment_id, req.risk_level.value)
        return req

    def wait_for_decision(self, request_id: str) -> ApprovalRequest:
        """Block until a decision is made or timeout expires."""
        event = self._events.get(request_id)
        if event is None:
            raise KeyError(f"Request {request_id} not found")

        resolved = event.wait(timeout=self._timeout)
        with self._lock:
            req = self._queue[request_id]
            if not resolved:
                req.status = ApprovalStatus.TIMEOUT
                # On timeout, auto-escalate: approve only non-critical safe actions
                req.approved_actions = [
                    a for a in req.proposed_actions
                    if a in _SAFE_AUTO_APPROVE
                ]
                logger.warning("HITL timeout for %s — auto-approved safe actions: %s",
                               request_id, [a.value for a in req.approved_actions])
            return req

    # ------------------------------------------------------------------
    # Decision entry points (called by dashboard / operator)
    # ------------------------------------------------------------------

    def approve(
        self,
        request_id: str,
        operator: str,
        approved_actions: Optional[List[RecommendedAction]] = None,
        notes: str = "",
    ) -> ApprovalRequest:
        with self._lock:
            req = self._queue[request_id]
            req.approved_actions = approved_actions if approved_actions is not None \
                                   else req.proposed_actions
            req.status     = (ApprovalStatus.PARTIAL
                              if approved_actions and set(approved_actions) != set(req.proposed_actions)
                              else ApprovalStatus.APPROVED)
            req.decided_by  = operator
            req.decided_at  = datetime.now(timezone.utc)
            req.notes       = notes
        self._events[request_id].set()
        logger.info("HITL %s: %s by %s", request_id, req.status.value, operator)
        return req

    def reject(self, request_id: str, operator: str, notes: str = "") -> ApprovalRequest:
        with self._lock:
            req = self._queue[request_id]
            req.status      = ApprovalStatus.REJECTED
            req.approved_actions = []
            req.decided_by  = operator
            req.decided_at  = datetime.now(timezone.utc)
            req.notes       = notes
        self._events[request_id].set()
        logger.info("HITL %s: REJECTED by %s", request_id, operator)
        return req

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def pending(self) -> List[ApprovalRequest]:
        with self._lock:
            return [r for r in self._queue.values()
                    if r.status == ApprovalStatus.PENDING]

    def get(self, request_id: str) -> Optional[ApprovalRequest]:
        return self._queue.get(request_id)

    def all_requests(self) -> List[ApprovalRequest]:
        with self._lock:
            return list(self._queue.values())

    def clear(self, *, pending_only: bool = True) -> int:
        """
        Clear requests from the queue.

        By default, only clears PENDING requests (useful for demos/UI resets).
        Returns the number of removed requests.
        """
        with self._lock:
            if not pending_only:
                n = len(self._queue)
                self._queue.clear()
                self._events.clear()
                return n

            to_delete = [rid for rid, r in self._queue.items() if r.status == ApprovalStatus.PENDING]
            for rid in to_delete:
                self._queue.pop(rid, None)
                self._events.pop(rid, None)
            return len(to_delete)


# Actions that can be auto-approved on timeout (low-risk, non-destructive)
_SAFE_AUTO_APPROVE = {
    RecommendedAction.MONITOR_ONLY,
    RecommendedAction.ALERT_OPERATIONS,
    RecommendedAction.NOTIFY_HOSPITAL,
}
