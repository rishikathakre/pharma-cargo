"""
AuditLogger
-----------
Writes immutable, append-only audit records to a JSONL file.
Every significant event in the pipeline (ingest, anomaly, risk assessment,
HITL decision, action execution, compliance violation) must pass through here.

Format: one JSON object per line, each with:
  - event_type  : string identifier
  - timestamp   : ISO-8601 UTC
  - shipment_id : (when applicable)
  - payload     : event-specific data

GDP/FDA compliance requires that audit logs are:
  - Tamper-evident (production: use a write-once store or digital signatures)
  - Retained for ≥ 5 years (21 CFR Part 211.68)
  - Attributable, legible, contemporaneous, original, accurate (ALCOA+)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config import AUDIT_LOG_PATH

logger = logging.getLogger(__name__)


class AuditLogger:
    """Thread-safe append-only JSONL audit logger."""

    def __init__(self, log_path: str = AUDIT_LOG_PATH):
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        logger.info("AuditLogger initialised → %s", self._path)

    # ------------------------------------------------------------------
    # Convenience log methods
    # ------------------------------------------------------------------

    def log_assessment(self, assessment: Any) -> None:
        self._write("RISK_ASSESSMENT", assessment.shipment_id, assessment.to_dict())

    def log_action_result(self, result: Any) -> None:
        self._write("ACTION_RESULT", None, result.to_dict())

    def log_compliance_violation(self, shipment_id: str, violation: str) -> None:
        self._write("COMPLIANCE_VIOLATION", shipment_id, {"violation": violation})

    def log_hitl_decision(self, request: Any) -> None:
        self._write("HITL_DECISION", request.shipment_id, request.to_dict())

    def log_pipeline_run(self, summary: Dict[str, Any]) -> None:
        self._write("PIPELINE_RUN", summary.get("shipment_id"), summary)

    def log_anomaly(self, anomaly: Any) -> None:
        self._write("ANOMALY_DETECTED", anomaly.shipment_id, anomaly.to_dict())

    def log_raw(self, event_type: str, shipment_id: Optional[str],
                payload: Dict[str, Any]) -> None:
        self._write(event_type, shipment_id, payload)

    # ------------------------------------------------------------------
    # Query helpers (for compliance reporting)
    # ------------------------------------------------------------------

    def get_shipment_history(self, shipment_id: str) -> list:
        records = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("shipment_id") == shipment_id:
                            records.append(record)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        return records

    def get_all_records(self, event_type: Optional[str] = None) -> list:
        records = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if event_type is None or record.get("event_type") == event_type:
                            records.append(record)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        return records

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(
        self,
        event_type: str,
        shipment_id: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        record = {
            "event_type":  event_type,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "shipment_id": shipment_id,
            "payload":     payload,
        }
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line)
