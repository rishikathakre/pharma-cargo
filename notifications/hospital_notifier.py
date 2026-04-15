"""
HospitalNotifier
----------------
Sends structured alerts to healthcare providers when shipment delays or
product integrity issues threaten vaccination schedules.

Notification payload follows HL7 FHIR-inspired structure so it can be
ingested by hospital EHR systems.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

from config import HOSPITAL_WEBHOOK_URL

logger = logging.getLogger(__name__)

try:
    from data.dataset_loader import loader as _dataset_loader
    _VACCINATION_DEMAND = _dataset_loader.vaccination_demand
    _HIGH_PRIORITY_LOCS = set(_dataset_loader.get_high_priority_locations())
except Exception:
    _VACCINATION_DEMAND = {}
    _HIGH_PRIORITY_LOCS = set()


class HospitalNotifier:
    def __init__(self, webhook_url: str = HOSPITAL_WEBHOOK_URL):
        self._url = webhook_url

    def notify(self, assessment: Any) -> Dict[str, Any]:
        """Build and dispatch a hospital alert for a risk assessment."""
        payload = self._build_payload(assessment)
        response = self._send(payload)
        logger.info("[%s] Hospital notification sent → %s",
                    assessment.shipment_id, response.get("status"))
        return response

    def notify_appointment_reschedule(
        self,
        shipment_id: str,
        delay_hours: float,
        affected_vaccines: list,
        clinic_id: str,
    ) -> Dict[str, Any]:
        """Specific notification for patient appointment rescheduling."""
        payload = {
            "notification_type": "APPOINTMENT_RESCHEDULE",
            "shipment_id":       shipment_id,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
            "clinic_id":         clinic_id,
            "delay_hours":       delay_hours,
            "affected_vaccines": affected_vaccines,
            "message": (
                f"Vaccine shipment {shipment_id} is delayed by {delay_hours:.1f} hours. "
                f"Please reschedule affected appointments for: "
                f"{', '.join(affected_vaccines)}."
            ),
            "urgency": "HIGH" if delay_hours > 12 else "MEDIUM",
        }
        return self._send(payload)

    # ------------------------------------------------------------------

    def _build_payload(self, assessment: Any) -> Dict[str, Any]:
        # Determine destination from assessment metadata if available
        destination = getattr(assessment, "metadata", {}).get("destination", "")
        vax_priority = self._get_vaccination_priority(destination)

        return {
            "notification_type":    "SHIPMENT_ALERT",
            "shipment_id":          assessment.shipment_id,
            "container_id":         assessment.container_id,
            "timestamp":            datetime.now(timezone.utc).isoformat(),
            "risk_level":           assessment.risk_level.value,
            "risk_score":           round(assessment.risk_score, 4),
            "spoilage_probability": round(assessment.spoilage_prob, 4),
            "recommended_actions":  [a.value for a in assessment.actions],
            "justification":        assessment.justification,
            "anomaly_summary": [
                {"type": an.anomaly_type.value, "severity": an.severity.value,
                 "description": an.description}
                for an in assessment.anomalies
            ],
            "vaccination_priority": vax_priority,
            "regulatory_context":   "GDP §9.2 | 21 CFR 600.15",
        }

    def _get_vaccination_priority(self, destination: str) -> Dict[str, Any]:
        """Return vaccination demand context for the destination."""
        if not destination or not _VACCINATION_DEMAND:
            return {"tier": "STANDARD", "location": destination or "unknown"}

        # Try exact match, then partial match (e.g. "New York" → "New York State")
        demand = _VACCINATION_DEMAND.get(destination)
        if not demand:
            for loc, d in _VACCINATION_DEMAND.items():
                if destination.lower() in loc.lower() or loc.lower() in destination.lower():
                    demand = d
                    break

        if demand:
            return {
                "tier":              demand.priority_tier,
                "location":          demand.location,
                "daily_vaccinations": demand.daily_vaccinations,
                "escalate":          demand.priority_tier in ("CRITICAL", "HIGH"),
            }
        return {"tier": "STANDARD", "location": destination, "escalate": False}

    def _send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """HTTP POST to hospital webhook.  Falls back to dry-run if unreachable."""
        try:
            data = json.dumps(payload).encode("utf-8")
            req  = urllib.request.Request(
                self._url,
                data    = data,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8")
                return {"status": "sent", "http_status": resp.status, "body": body}
        except Exception as exc:
            logger.warning("Hospital webhook unreachable (%s) — dry-run mode", exc)
            return {"status": "dry_run", "payload_size": len(json.dumps(payload))}
