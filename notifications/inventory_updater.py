"""
InventoryUpdater
----------------
Updates downstream inventory forecasts and triggers cold-storage or
quarantine interventions when product viability is at risk.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

from config import INVENTORY_API_URL

logger = logging.getLogger(__name__)

# TODO: Replace with product catalogue lookup (by product_id / NDC code).
#   These are order-of-magnitude placeholders only — see insurance_docs.py for context.
DOSES_PER_CONTAINER = 5_000   # placeholder — varies by product and packaging
DOSE_VALUE_USD      = 25.0    # USD per dose — placeholder


class InventoryUpdater:
    def __init__(self, api_url: str = INVENTORY_API_URL):
        self._url = api_url

    def update_cold_storage(self, assessment: Any) -> Dict[str, Any]:
        """Request an emergency cold-storage slot near the container's location."""
        doses_at_risk = round(assessment.spoilage_prob * DOSES_PER_CONTAINER)
        value_at_risk = round(doses_at_risk * DOSE_VALUE_USD, 2)
        payload = {
            "action":         "REQUEST_COLD_STORAGE",
            "shipment_id":    assessment.shipment_id,
            "container_id":   assessment.container_id,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "risk_score":     round(assessment.risk_score, 4),
            "justification":  assessment.justification,
            "urgency":        "CRITICAL" if assessment.risk_score > 0.85 else "HIGH",
            "doses_at_risk":  doses_at_risk,
            "value_at_risk_usd": value_at_risk,
        }
        response = self._post(payload)
        logger.info("[%s] Cold-storage request sent → %s",
                    assessment.shipment_id, response.get("status"))
        return response

    def quarantine(self, assessment: Any) -> Dict[str, Any]:
        """Mark shipment as quarantined in inventory system."""
        payload = {
            "action":           "QUARANTINE",
            "shipment_id":      assessment.shipment_id,
            "container_id":     assessment.container_id,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "reason":           assessment.justification,
            "spoilage_prob":    round(assessment.spoilage_prob, 4),
            "regulatory_refs":  ["GDP §9.2", "21 CFR 211.142"],
        }
        response = self._post(payload)
        logger.info("[%s] Quarantine flag set → %s",
                    assessment.shipment_id, response.get("status"))
        return response

    def update_forecast(
        self,
        shipment_id: str,
        delay_hours: float,
        spoilage_prob: float,
        product_ids: list,
    ) -> Dict[str, Any]:
        """Adjust inventory forecasts for downstream clinics."""
        doses_at_risk    = round(spoilage_prob * DOSES_PER_CONTAINER)
        expected_arrival = "DELAYED" if delay_hours > 0 else "ON_SCHEDULE"
        if spoilage_prob >= 1.0:
            adjustment = "ZERO_OUT"
        elif spoilage_prob > 0.5:
            adjustment = "REDUCE"
        else:
            adjustment = "DELAY"

        payload = {
            "action":            "UPDATE_FORECAST",
            "shipment_id":       shipment_id,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
            "delay_hours":       delay_hours,
            "spoilage_prob":     round(spoilage_prob, 4),
            "product_ids":       product_ids,
            "adjustment":        adjustment,
            "expected_arrival":  expected_arrival,
            "doses_at_risk":     doses_at_risk,
            "estimated_loss_usd": round(doses_at_risk * DOSE_VALUE_USD, 2),
            "downstream_note": (
                "Downstream clinics should update stock projections and "
                "identify alternative supply sources if spoilage_prob > 0.5."
            ),
        }
        return self._post(payload)

    # ------------------------------------------------------------------

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            data = json.dumps(payload).encode("utf-8")
            req  = urllib.request.Request(
                self._url,
                data    = data,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return {"status": "sent", "http_status": resp.status}
        except Exception as exc:
            logger.warning("Inventory API unreachable (%s) — dry-run mode", exc)
            return {"status": "dry_run", "action": payload.get("action")}
