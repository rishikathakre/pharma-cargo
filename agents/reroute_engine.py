"""
RerouteEngine
-------------
Evaluates and selects the safest rerouting path when a pharmaceutical
shipment is at critical risk.

Three-path decision model:
  PATH A — Cold-Storage Rescue   : divert to nearest GDP-compliant facility
  PATH B — Last-Mile Courier     : switch to pharma-certified emergency carrier
  PATH C — Original Route        : keep current path if ETA is still safe

Each path is scored against:
  ETA (hours to product reaching cold chain)  vs
  Time-to-Spoilage (hours before product is unviable)

The path with the smallest ETA that is still less than Time-to-Spoilage wins.
If no path is viable, QUARANTINE is escalated.

Also provides:
  - suggest()            : lightweight carrier-swap recommendation (existing)
  - escalate_customs()   : structured P1/P2 customs escalation (existing)
  - plan_reroute()       : full 3-path evaluation returning a ReroutePlan
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from data.dataset_loader import loader as _dataset_loader
    _CARRIER_PROFILES = _dataset_loader.carrier_profiles
except Exception:
    _CARRIER_PROFILES = {}

# ---------------------------------------------------------------------------
# GDP-compliant cold-storage facilities near major pharma hub airports.
# In production this would be a live API query (e.g. WorldCourier, Sensitech).
# Format: { "region_key": (name, avg_divert_hours) }
# avg_divert_hours = estimated time from diversion decision to cargo secured.
# ---------------------------------------------------------------------------
_COLD_STORAGE_FACILITIES: Dict[str, Tuple[str, float]] = {
    # North America
    "JFK":  ("World Courier JFK — Jamaica, NY (GDP-certified)",          1.5),
    "LAX":  ("Cryoport Los Angeles Cold Hub (GDP/IATA CEIV)",            2.0),
    "ORD":  ("AmerisourceBergen Chicago Cold Chain (GDP-certified)",      2.0),
    "MIA":  ("Marken Miami Pharma Hub (GDP/GDP+)",                        1.5),
    "BOS":  ("Cardinal Health Boston (GDP-certified)",                    2.0),
    # Europe
    "FRA":  ("Lufthansa Cargo Cool Center Frankfurt (IATA CEIV Pharma)", 1.0),
    "AMS":  ("Schiphol Pharma Hub Amsterdam (GDP-certified)",             1.5),
    "CDG":  ("Paris CDG Pharma Corridor (IATA CEIV)",                     2.0),
    "LHR":  ("Heathrow Pharma Zone (MHRA-licensed)",                      1.5),
    "ZRH":  ("Kuehne+Nagel Zurich Pharma (GDP-certified)",                1.5),
    # Asia-Pacific
    "SIN":  ("Singapore Changi Pharma Hub (HSA-licensed, GDP)",           2.0),
    "HKG":  ("Hong Kong HACTL Pharma (GDP-certified)",                    2.5),
    "PVG":  ("Shanghai Pudong Pharma Cold Hub (GDP-certified)",           3.0),
    "BOM":  ("Mumbai CSIA Pharma Zone (CDSCO-licensed)",                  2.5),
    "NRT":  ("Tokyo Narita Nippon Express Pharma (PMDA-licensed)",        2.0),
}

_DEFAULT_FACILITY = ("Nearest GDP-Compliant Cold Hub (auto-located)", 3.0)

# Emergency pharma courier baseline ETA (hours from pickup to cold-chain handoff)
_EMERGENCY_COURIER_ETA_HOURS = 4.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PathEvaluation:
    """Result of evaluating one reroute path."""
    path:               str         # "COLD_STORAGE" | "LAST_MILE_COURIER" | "ORIGINAL_ROUTE"
    eta_hours:          float       # hours until cargo reaches cold chain / destination
    viable:             bool        # True if eta_hours < time_to_spoilage_hours
    margin_hours:       float       # time_to_spoilage - eta (positive = safe buffer)
    carrier:            Optional[str] = None
    facility:           Optional[str] = None
    notes:              str = ""


@dataclass
class ReroutePlan:
    """
    Full output of plan_reroute().
    Consumed by cascade_orchestrator._handle_reroute_shipment()
    and passed to hospital_notifier.notify_reroute().
    """
    shipment_id:            str
    generated_at:           str
    chosen_path:            str         # winning path label
    eta_hours:              float       # ETA of chosen path
    time_to_spoilage_hours: float       # hours until product is unviable
    margin_hours:           float       # safety margin (positive = still safe)
    viable:                 bool        # False → escalate to QUARANTINE
    paths_evaluated:        List[PathEvaluation] = field(default_factory=list)
    recommended_carrier:    Optional[str] = None
    cold_storage_facility:  Optional[str] = None
    urgency:                str = "HIGH"
    rationale:              str = ""
    hospital_eta_note:      str = ""    # ready-to-send string for hospital alert
    regulatory_note:        str = (
        "Carrier substitution must be documented per GDP §7.2 "
        "(Qualification of Suppliers) before transfer of custody. "
        "Cold-storage diversion logged per GDP §9.2."
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action":                  "REROUTE_SHIPMENT",
            "shipment_id":             self.shipment_id,
            "generated_at":            self.generated_at,
            "chosen_path":             self.chosen_path,
            "eta_hours":               round(self.eta_hours, 2),
            "time_to_spoilage_hours":  round(self.time_to_spoilage_hours, 2),
            "margin_hours":            round(self.margin_hours, 2),
            "viable":                  self.viable,
            "recommended_carrier":     self.recommended_carrier,
            "cold_storage_facility":   self.cold_storage_facility,
            "urgency":                 self.urgency,
            "rationale":               self.rationale,
            "hospital_eta_note":       self.hospital_eta_note,
            "regulatory_note":         self.regulatory_note,
            "paths_evaluated": [
                {
                    "path":         p.path,
                    "eta_hours":    round(p.eta_hours, 2),
                    "viable":       p.viable,
                    "margin_hours": round(p.margin_hours, 2),
                    "carrier":      p.carrier,
                    "facility":     p.facility,
                    "notes":        p.notes,
                }
                for p in self.paths_evaluated
            ],
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class RerouteEngine:
    """
    Evaluates reroute paths and selects the safest option for a
    pharmaceutical shipment at critical or high risk.
    """

    # ------------------------------------------------------------------
    # Primary method: full 3-path reroute planner
    # ------------------------------------------------------------------

    def plan_reroute(self, assessment: Any) -> ReroutePlan:
        """
        Evaluate PATH A (cold-storage), PATH B (last-mile courier),
        PATH C (original route) and return the best viable plan.

        Decision rule:
          1. Compute time_to_spoilage from current spoilage_prob + product profile
          2. Evaluate each path's ETA
          3. Choose the path with shortest ETA that is still < time_to_spoilage
          4. If no path is viable, mark plan as not viable (caller should QUARANTINE)
        """
        metadata    = getattr(assessment, "metadata", {})
        origin      = metadata.get("origin",      "Unknown")
        destination = metadata.get("destination", "Unknown")
        product_id  = metadata.get("product_id",  "VACC-STANDARD")

        tts = self._time_to_spoilage(assessment)
        delay_hours = self._extract_delay_hours(assessment)

        path_a = self._eval_cold_storage(assessment, destination, tts)
        path_b = self._eval_last_mile(assessment, tts)
        path_c = self._eval_original_route(assessment, delay_hours, tts)

        # Pick the viable path with the lowest ETA
        viable_paths = [p for p in [path_a, path_b, path_c] if p.viable]
        viable_paths.sort(key=lambda p: p.eta_hours)

        if viable_paths:
            best = viable_paths[0]
        else:
            # Nothing viable — fallback to least-bad option
            all_paths = sorted([path_a, path_b, path_c], key=lambda p: p.eta_hours)
            best = all_paths[0]

        chosen_carrier = best.carrier
        chosen_facility = best.facility
        urgency = self._urgency(assessment.risk_score)
        rationale = self._build_plan_rationale(
            assessment, tts, delay_hours, best, viable_paths
        )
        hospital_note = self._build_hospital_eta_note(
            assessment, best, tts, destination
        )

        plan = ReroutePlan(
            shipment_id            = assessment.shipment_id,
            generated_at           = datetime.now(timezone.utc).isoformat(),
            chosen_path            = best.path,
            eta_hours              = best.eta_hours,
            time_to_spoilage_hours = tts,
            margin_hours           = best.margin_hours,
            viable                 = best.viable,
            paths_evaluated        = [path_a, path_b, path_c],
            recommended_carrier    = chosen_carrier,
            cold_storage_facility  = chosen_facility,
            urgency                = urgency,
            rationale              = rationale,
            hospital_eta_note      = hospital_note,
        )

        logger.info(
            "[%s] Reroute plan: path=%s  ETA=%.1fh  TtS=%.1fh  margin=%.1fh  viable=%s",
            assessment.shipment_id,
            best.path, best.eta_hours, tts, best.margin_hours, best.viable,
        )
        return plan

    # ------------------------------------------------------------------
    # Existing methods (kept for backward compatibility)
    # ------------------------------------------------------------------

    def suggest(self, assessment: Any) -> Dict[str, Any]:
        """
        Lightweight carrier-swap recommendation.
        Used by ActionAgent as the REROUTE_SHIPMENT handler.
        Now delegates to plan_reroute() for richer output.
        """
        plan = self.plan_reroute(assessment)
        return plan.to_dict()

    def escalate_customs(self, assessment: Any) -> Dict[str, Any]:
        """Build a structured customs escalation request."""
        metadata    = getattr(assessment, "metadata", {})
        destination = metadata.get("destination", "Unknown")
        delay_hours = self._extract_delay_hours(assessment)

        payload = {
            "action":              "CUSTOMS_ESCALATION",
            "shipment_id":         assessment.shipment_id,
            "container_id":        assessment.container_id,
            "generated_at":        datetime.now(timezone.utc).isoformat(),
            "destination":         destination,
            "risk_level":          assessment.risk_level.value,
            "risk_score":          round(assessment.risk_score, 4),
            "current_delay_hours": round(delay_hours, 1),
            "priority_code": (
                "P1-PHARMACEUTICAL" if assessment.risk_score > 0.7
                else "P2-PHARMACEUTICAL"
            ),
            "justification":    assessment.justification,
            "regulatory_basis": [
                "GDP §7.1 – Import Operations require expedited clearance "
                "for temperature-sensitive pharmaceuticals",
                "21 CFR 312.3 – Biologics subject to FDA priority release",
            ],
            "requested_action": (
                "REQUEST PRIORITY CUSTOMS RELEASE — pharmaceutical cold-chain "
                f"product at risk after {delay_hours:.1f}h delay. "
                "Temperature excursion may compromise vaccine viability."
            ),
        }
        logger.info(
            "[%s] Customs escalation: priority=%s  delay=%.1fh",
            assessment.shipment_id, payload["priority_code"], delay_hours,
        )
        return payload

    # ------------------------------------------------------------------
    # Path evaluators
    # ------------------------------------------------------------------

    def _eval_cold_storage(
        self, assessment: Any, destination: str, tts: float
    ) -> PathEvaluation:
        """
        PATH A: Divert to nearest GDP-compliant cold-storage facility.
        ETA = time to get cargo off current transport + into cold chain.
        """
        facility_name, eta = self._nearest_facility(destination)
        margin = tts - eta
        return PathEvaluation(
            path     = "COLD_STORAGE",
            eta_hours= eta,
            viable   = eta < tts,
            margin_hours = margin,
            facility = facility_name,
            notes    = (
                f"Divert to {facility_name}. "
                f"Est. {eta:.1f}h to cold chain. "
                f"Spoilage in {tts:.1f}h — margin {margin:+.1f}h."
            ),
        )

    def _eval_last_mile(self, assessment: Any, tts: float) -> PathEvaluation:
        """
        PATH B: Switch to an emergency pharma-certified last-mile courier.
        ETA = best available pharma-certified carrier avg_delay_hours,
              clamped to _EMERGENCY_COURIER_ETA_HOURS minimum.
        """
        metadata        = getattr(assessment, "metadata", {})
        current_carrier = metadata.get("carrier", "")
        alternatives    = self._rank_alternatives(current_carrier)

        if alternatives:
            best_alt  = alternatives[0]
            eta       = max(best_alt["avg_delay_hours"], _EMERGENCY_COURIER_ETA_HOURS)
            carrier   = best_alt["carrier"]
            rel_score = best_alt["reliability_score"]
            note = (
                f"Switch to {carrier} (reliability={rel_score:.2f}, "
                f"avg_delay={best_alt['avg_delay_hours']:.1f}h). "
                f"Est. handoff in {eta:.1f}h."
            )
        else:
            eta     = _EMERGENCY_COURIER_ETA_HOURS
            carrier = "Emergency Pharma Courier (generic)"
            note    = f"No carrier profiles loaded — using {eta:.1f}h generic estimate."

        margin = tts - eta
        return PathEvaluation(
            path     = "LAST_MILE_COURIER",
            eta_hours= eta,
            viable   = eta < tts,
            margin_hours = margin,
            carrier  = carrier,
            notes    = note,
        )

    def _eval_original_route(
        self, assessment: Any, delay_hours: float, tts: float
    ) -> PathEvaluation:
        """
        PATH C: Keep current carrier/route.
        ETA = remaining planned flight time + current delay accumulation.
        Conservative estimate: assume delays will continue at 0.5h/tick.
        """
        # Base estimate: current delay + 2h buffer for continued degradation
        eta    = delay_hours + 2.0
        margin = tts - eta
        viable = eta < tts and assessment.risk_score < 0.85  # never keep original if CRITICAL
        return PathEvaluation(
            path     = "ORIGINAL_ROUTE",
            eta_hours= eta,
            viable   = viable,
            margin_hours = margin,
            notes    = (
                f"Current delay: {delay_hours:.1f}h + 2h buffer = {eta:.1f}h ETA. "
                + ("Viable — spoilage margin OK." if viable
                   else "Not viable — risk too high or margin negative.")
            ),
        )

    # ------------------------------------------------------------------
    # Time-to-spoilage estimation
    # ------------------------------------------------------------------

    def _time_to_spoilage(self, assessment: Any) -> float:
        """
        Estimate hours remaining before product becomes unviable.

        Formula:
          remaining_safe_fraction = 1.0 - spoilage_prob
          excursion_budget_hours  = product.excursion_max_hours
          time_to_spoilage        = remaining_safe_fraction × excursion_budget_hours

        Floor at 0.5h (always at least 30 min of margin reported).
        Cap at 72h (conservative upper bound if no excursion yet).
        """
        spoilage_prob = getattr(assessment, "spoilage_prob", 0.0)
        product_id    = getattr(assessment, "metadata", {}).get("product_id", "VACC-STANDARD")

        try:
            from data.dataset_loader import loader
            product = loader.get_product(product_id)
            budget  = float(product.excursion_max_hours)
        except Exception:
            budget  = 72.0   # VACC-STANDARD default

        remaining = max(0.0, 1.0 - spoilage_prob) * budget

        # Arrhenius acceleration: if current temp is high, real rate is faster.
        # Apply a conservative 1.5× factor when spoilage_prob > 0.3.
        if spoilage_prob > 0.3:
            remaining /= 1.5

        return max(0.5, min(remaining, 72.0))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_delay_hours(self, assessment: Any) -> float:
        """Pull the largest delay_hours value from anomalies."""
        from agents.anomaly_agent import AnomalyType
        delay = 0.0
        for a in getattr(assessment, "anomalies", []):
            if hasattr(a, "anomaly_type") and a.anomaly_type in (
                AnomalyType.FLIGHT_DELAY, AnomalyType.FLIGHT_DIVERSION
            ):
                if a.measured_value:
                    delay = max(delay, float(a.measured_value))
        return delay

    def _nearest_facility(self, destination: str) -> Tuple[str, float]:
        """
        Return (facility_name, divert_hours) for the cold-storage hub
        closest to the destination airport/city.
        Checks for IATA code match first, then substring match.
        """
        dest_upper = destination.upper()
        # Direct IATA match
        if dest_upper in _COLD_STORAGE_FACILITIES:
            return _COLD_STORAGE_FACILITIES[dest_upper]
        # Substring match (e.g. "California" → LAX)
        for code, (name, hours) in _COLD_STORAGE_FACILITIES.items():
            if code in dest_upper or dest_upper in name.upper():
                return name, hours
        return _DEFAULT_FACILITY

    def _rank_alternatives(self, current_carrier: str) -> List[Dict[str, Any]]:
        """Return up to 3 alternative carriers ranked by reliability."""
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

    def _urgency(self, risk_score: float) -> str:
        if risk_score >= 0.85: return "EMERGENCY"
        if risk_score >= 0.70: return "HIGH"
        if risk_score >= 0.40: return "MEDIUM"
        return "LOW"

    def _build_plan_rationale(
        self,
        assessment: Any,
        tts: float,
        delay_hours: float,
        best: PathEvaluation,
        viable_paths: List[PathEvaluation],
    ) -> str:
        parts = [
            f"Shipment {assessment.shipment_id} — "
            f"risk={assessment.risk_level.value} (score={assessment.risk_score:.2f}), "
            f"spoilage_prob={assessment.spoilage_prob:.2f}, "
            f"time_to_spoilage={tts:.1f}h, "
            f"current_delay={delay_hours:.1f}h.",
        ]
        if viable_paths:
            parts.append(
                f"{len(viable_paths)} viable path(s) found. "
                f"Selected '{best.path}' (ETA={best.eta_hours:.1f}h, "
                f"margin={best.margin_hours:+.1f}h)."
            )
        else:
            parts.append(
                "No viable path found — all ETAs exceed time-to-spoilage. "
                "Quarantine escalation recommended."
            )
        if best.carrier:
            parts.append(f"Carrier: {best.carrier}.")
        if best.facility:
            parts.append(f"Cold-storage: {best.facility}.")
        return " ".join(parts)

    def _build_hospital_eta_note(
        self,
        assessment: Any,
        best: PathEvaluation,
        tts: float,
        destination: str,
    ) -> str:
        status = "ON TRACK" if best.viable else "AT RISK OF LOSS"
        path_label = {
            "COLD_STORAGE":      "diverted to emergency cold-storage",
            "LAST_MILE_COURIER": "transferred to emergency pharma courier",
            "ORIGINAL_ROUTE":    "continuing on original route with monitoring",
        }.get(best.path, best.path)

        return (
            f"REROUTE ALERT — Shipment {assessment.shipment_id} has been {path_label}. "
            f"New estimated arrival: {best.eta_hours:.1f} hours from now. "
            f"Product status: {status} "
            f"(spoilage probability {assessment.spoilage_prob:.0%}, "
            f"time-to-spoilage window: {tts:.1f}h). "
            f"Destination: {destination}. "
            f"Please update patient scheduling and inventory forecasts accordingly. "
            f"GDP §9.2 diversion logged — full audit trail available on request."
        )
