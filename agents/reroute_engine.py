"""
RerouteEngine
-------------
Recommends alternative carriers and routes when a shipment is at risk.
Uses real carrier performance data from DatasetLoader to rank alternatives.

Returns a structured reroute recommendation that includes:
  - Current carrier context (reliability score, avg delay)
  - Ranked alternative carriers with estimated time savings
  - Customs escalation priority if customs hold is active
  - Urgency level tied to risk score
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from data.dataset_loader import loader as _dataset_loader
    _CARRIER_PROFILES = _dataset_loader.carrier_profiles
except Exception:
    _CARRIER_PROFILES = {}


class RerouteEngine:
    """
    Produces data-driven reroute recommendations from carrier performance data.
    """

    def suggest(self, assessment: Any) -> Dict[str, Any]:
        """
        Build a reroute recommendation for a risk assessment.
        Called by ActionAgent when REROUTE_SHIPMENT is approved.
        """
        metadata      = getattr(assessment, "metadata", {})
        current_name  = metadata.get("carrier", "Unknown")
        destination   = metadata.get("destination", "Unknown")
        origin        = metadata.get("origin",      "Unknown")

        current_profile = _CARRIER_PROFILES.get(current_name)
        alternatives    = self._rank_alternatives(current_name)

        urgency = self._urgency(assessment.risk_score)
        rationale = self._build_rationale(assessment, current_profile, alternatives)

        recommendation = {
            "action":           "REROUTE_SHIPMENT",
            "shipment_id":      assessment.shipment_id,
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "urgency":          urgency,
            "route": {
                "origin":      origin,
                "destination": destination,
            },
            "current_carrier": self._carrier_summary(current_name, current_profile),
            "recommended_alternatives": alternatives,
            "rationale":        rationale,
            "regulatory_note":  (
                "Carrier substitution must be documented per GDP §7.2 "
                "(Qualification of Suppliers) before transfer of custody."
            ),
        }

        logger.info(
            "[%s] Reroute recommendation: %s → top alternative: %s (score %.2f)",
            assessment.shipment_id,
            current_name,
            alternatives[0]["carrier"] if alternatives else "N/A",
            alternatives[0]["reliability_score"] if alternatives else 0,
        )
        return recommendation

    def escalate_customs(self, assessment: Any) -> Dict[str, Any]:
        """
        Build a structured customs escalation request.
        Called when CUSTOMS_ESCALATION action is approved.
        """
        metadata    = getattr(assessment, "metadata", {})
        destination = metadata.get("destination", "Unknown")

        # Find delay hours from anomalies
        delay_hours = 0.0
        for a in getattr(assessment, "anomalies", []):
            if hasattr(a, "measured_value") and a.measured_value:
                delay_hours = max(delay_hours, a.measured_value)

        payload = {
            "action":           "CUSTOMS_ESCALATION",
            "shipment_id":      assessment.shipment_id,
            "container_id":     assessment.container_id,
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "destination":      destination,
            "risk_level":       assessment.risk_level.value,
            "risk_score":       round(assessment.risk_score, 4),
            "current_delay_hours": round(delay_hours, 1),
            "priority_code":    "P1-PHARMACEUTICAL" if assessment.risk_score > 0.7 else "P2-PHARMACEUTICAL",
            "justification":    assessment.justification,
            "regulatory_basis": [
                "GDP §7.1 – Import Operations require expedited clearance for "
                "temperature-sensitive pharmaceuticals",
                "21 CFR 312.3 – Biologics subject to FDA priority release",
            ],
            "requested_action": (
                "REQUEST PRIORITY CUSTOMS RELEASE — pharmaceutical cold-chain "
                f"product at risk after {delay_hours:.1f}h delay. "
                "Temperature excursion may compromise vaccine viability."
            ),
        }

        logger.info(
            "[%s] Customs escalation submitted: priority=%s, delay=%.1fh",
            assessment.shipment_id, payload["priority_code"], delay_hours,
        )
        return payload

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rank_alternatives(self, current_carrier: str) -> List[Dict[str, Any]]:
        """Return up to 3 alternative carriers ranked by reliability, excluding current."""
        ranked = sorted(
            [p for name, p in _CARRIER_PROFILES.items() if name != current_carrier],
            key=lambda p: p.reliability_score,
            reverse=True,
        )
        current = _CARRIER_PROFILES.get(current_carrier)
        current_delay = current.avg_delay_hours if current else 4.0

        return [
            {
                "carrier":            p.name,
                "reliability_score":  p.reliability_score,
                "avg_delay_hours":    p.avg_delay_hours,
                "damage_claims":      p.damage_claims,
                "est_time_saving_hours": round(
                    max(0.0, current_delay - p.avg_delay_hours), 2
                ),
            }
            for p in ranked[:3]
        ]

    def _carrier_summary(self, name: str, profile: Any) -> Dict[str, Any]:
        if profile is None:
            return {"carrier": name, "reliability_score": "unknown"}
        return {
            "carrier":           name,
            "reliability_score": profile.reliability_score,
            "avg_delay_hours":   profile.avg_delay_hours,
            "damage_claims":     profile.damage_claims,
        }

    def _urgency(self, risk_score: float) -> str:
        if risk_score >= 0.85:
            return "EMERGENCY"
        if risk_score >= 0.70:
            return "HIGH"
        if risk_score >= 0.40:
            return "MEDIUM"
        return "LOW"

    def _build_rationale(
        self,
        assessment: Any,
        current_profile: Any,
        alternatives: List[Dict],
    ) -> str:
        parts = [
            f"Shipment {assessment.shipment_id} has risk score "
            f"{assessment.risk_score:.2f} ({assessment.risk_level.value})."
        ]
        if current_profile:
            parts.append(
                f"Current carrier reliability score: {current_profile.reliability_score:.2f} "
                f"(avg delay {current_profile.avg_delay_hours:.1f}h, "
                f"{current_profile.damage_claims} damage claims)."
            )
        if alternatives:
            best = alternatives[0]
            parts.append(
                f"Recommended alternative: {best['carrier']} "
                f"(score {best['reliability_score']:.2f}, "
                f"avg delay {best['avg_delay_hours']:.1f}h, "
                f"est. saving {best['est_time_saving_hours']:.1f}h)."
            )
        return " ".join(parts)
