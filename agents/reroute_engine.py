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

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE

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

# GPS coordinates for each cold-storage facility (for distance-based selection)
_FACILITY_COORDS: Dict[str, Tuple[float, float]] = {
    "JFK": (40.6413, -73.7781),
    "LAX": (33.9425, -118.4081),
    "ORD": (41.9742, -87.9073),
    "MIA": (25.7959, -80.2870),
    "BOS": (42.3656, -71.0096),
    "FRA": (50.0379,   8.5622),
    "AMS": (52.3086,   4.7639),
    "CDG": (49.0097,   2.5479),
    "LHR": (51.4700,  -0.4543),
    "ZRH": (47.4647,   8.5492),
    "SIN": ( 1.3644, 103.9915),
    "HKG": (22.3080, 113.9185),
    "PVG": (31.1443, 121.8083),
    "BOM": (19.0896,  72.8656),
    "NRT": (35.7647, 140.3864),
}


def _haversine_km_coords(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) tuples."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))

# Map common full airport names (as used by map_sim) → IATA codes for facility lookup.
_AIRPORT_NAME_TO_IATA: Dict[str, str] = {
    "jfk airport":              "JFK",
    "john f kennedy":           "JFK",
    "heathrow airport":         "LHR",
    "london heathrow":          "LHR",
    "frankfurt airport":        "FRA",
    "frankfurt am main":        "FRA",
    "mumbai airport":           "BOM",
    "chhatrapati shivaji":      "BOM",
    "o'hare airport":           "ORD",
    "ohare airport":            "ORD",
    "chicago o'hare":           "ORD",
    "pudong airport":           "PVG",
    "shanghai pudong":          "PVG",
    "singapore changi airport": "SIN",
    "changi airport":           "SIN",
    "los angeles":              "LAX",
    "lax airport":              "LAX",
    "miami airport":            "MIA",
    "miami international":      "MIA",
    "boston airport":           "BOS",
    "logan airport":            "BOS",
    "amsterdam airport":        "AMS",
    "schiphol":                 "AMS",
    "paris cdg":                "CDG",
    "charles de gaulle":        "CDG",
    "zurich airport":           "ZRH",
    "hong kong airport":        "HKG",
    "narita airport":           "NRT",
    "tokyo narita":             "NRT",
}

# Emergency pharma courier baseline ETA (hours from pickup to cold-chain handoff)
_EMERGENCY_COURIER_ETA_HOURS = 4.0

# ---------------------------------------------------------------------------
# Known US pharma hub airports with FULL IATA CEIV Pharma / GDP certification.
# All other US large airports default to PARTIAL (refrigerated handling only).
# Congestion tiers based on FAA traffic volume rankings.
# ---------------------------------------------------------------------------
_US_PHARMA_FULL: set = {
    "JFK", "EWR",  # New York metro — largest US pharma air hub
    "LAX",          # Los Angeles IATA CEIV Pharma certified
    "ORD",          # Chicago O'Hare AmerisourceBergen hub
    "MIA",          # Miami Marken / World Courier GDP+
    "BOS",          # Boston Cardinal Health
    "IAD",          # Washington Dulles pharma corridor
    "DFW",          # Dallas IATA CEIV Pharma
    "SFO",          # San Francisco bio/pharma cluster
    "PHL",          # Philadelphia pharma valley proximity
    "IAH",          # Houston — Life Sciences hub
    "SLC",          # Salt Lake City — cold-chain distribution hub
}

_US_CONGESTION: Dict[str, str] = {
    # HIGH — consistently slot-constrained
    "ATL": "HIGH", "ORD": "HIGH", "LAX": "HIGH", "DFW": "HIGH",
    "JFK": "HIGH", "SFO": "HIGH", "EWR": "HIGH", "LGA": "HIGH",
    "DEN": "HIGH", "CLT": "HIGH", "PHX": "HIGH",
    # MEDIUM
    "BOS": "MEDIUM", "IAD": "MEDIUM", "MIA": "MEDIUM", "PHL": "MEDIUM",
    "IAH": "MEDIUM", "MCO": "MEDIUM", "MSP": "MEDIUM", "DTW": "MEDIUM",
    "SEA": "MEDIUM", "MDW": "MEDIUM", "FLL": "MEDIUM", "BWI": "MEDIUM",
    "TPA": "MEDIUM", "PDX": "MEDIUM", "SAN": "MEDIUM", "SLC": "MEDIUM",
    "HNL": "MEDIUM", "SNA": "MEDIUM", "STL": "MEDIUM", "CVG": "MEDIUM",
    # LOW — everything else
}


def _load_us_airport_db() -> Dict[str, Dict]:
    """
    Load US large airports from data/raw/us-airports.csv.
    Returns a dict keyed by IATA code with name, city, lat, lon,
    cold_chain tier, and congestion level.
    Falls back to an empty dict if the file is missing.
    """
    import csv as _csv
    from pathlib import Path as _Path

    csv_path = _Path(__file__).resolve().parents[1] / "data" / "raw" / "us-airports.csv"
    if not csv_path.exists():
        logger.warning("us-airports.csv not found — airport divert lookup unavailable")
        return {}

    db: Dict[str, Dict] = {}
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            iata = (row.get("iata_code") or "").strip()
            if not iata or row.get("type", "") != "large_airport":
                continue
            try:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
            except (ValueError, KeyError):
                continue
            cold_chain = "FULL" if iata in _US_PHARMA_FULL else "PARTIAL"
            congestion = _US_CONGESTION.get(iata, "LOW")
            db[iata] = {
                "name":       row.get("name", iata),
                "city":       row.get("municipality", ""),
                "lat":        lat,
                "lon":        lon,
                "cold_chain": cold_chain,
                "congestion": congestion,
            }

    logger.info("Loaded %d US large airports for divert lookup", len(db))
    return db


# Module-level load — done once at import time.
_AIRPORT_DB: Dict[str, Dict] = _load_us_airport_db()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two lat/lon points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _airports_near(lat: float, lon: float, radius_km: float = 800, max_results: int = 6) -> List[Dict]:
    """
    Return up to max_results airports within radius_km sorted by distance.
    Each result dict has: iata, name, city, distance_km, cold_chain, congestion.
    """
    results = []
    for iata, info in _AIRPORT_DB.items():
        dist = _haversine_km(lat, lon, info["lat"], info["lon"])
        if dist <= radius_km:
            results.append({
                "iata":        iata,
                "name":        info["name"],
                "city":        info["city"],
                "distance_km": round(dist, 0),
                "cold_chain":  info["cold_chain"],
                "congestion":  info["congestion"],
            })
    results.sort(key=lambda x: x["distance_km"])
    return results[:max_results]


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
    iata:               Optional[str] = None   # IATA code of the cold-storage hub (COLD_STORAGE only)
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
    gemini_decision:        bool = False   # True when Gemini selected the path
    cold_storage_iata:      Optional[str] = None  # IATA code of chosen cold-storage hub
    # Gemini-recommended divert airport (populated when Gemini selects COLD_STORAGE)
    divert_airport_iata:    Optional[str] = None
    divert_airport_name:    Optional[str] = None
    divert_airport_city:    Optional[str] = None
    divert_distance_km:     Optional[float] = None

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
            "cold_storage_iata":       self.cold_storage_iata,
            "divert_airport_iata":     self.divert_airport_iata,
            "divert_airport_name":     self.divert_airport_name,
            "divert_airport_city":     self.divert_airport_city,
            "divert_distance_km":      self.divert_distance_km,
            "urgency":                 self.urgency,
            "rationale":               self.rationale,
            "hospital_eta_note":       self.hospital_eta_note,
            "regulatory_note":         self.regulatory_note,
            "gemini_decision":         self.gemini_decision,
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
            math_best = viable_paths[0]
        else:
            # Nothing viable — fallback to least-bad option
            all_paths = sorted([path_a, path_b, path_c], key=lambda p: p.eta_hours)
            math_best = all_paths[0]

        # Map path labels → PathEvaluation objects for LLM override resolution
        path_map = {
            "COLD_STORAGE":      path_a,
            "LAST_MILE_COURIER": path_b,
            "ORIGINAL_ROUTE":    path_c,
        }

        # --- Gemini decision point ---
        # Ask the LLM to select the best path with expert regulatory reasoning.
        # The math above narrows the field; Gemini makes (and explains) the call.
        # Falls back to math_best silently if no API key or network failure.
        llm_result     = self._llm_select_path(assessment, path_a, path_b, path_c, tts, delay_hours)
        gemini_decided = False

        divert_airport: Optional[Dict] = None

        if llm_result and len(llm_result) == 4:
            llm_path, llm_rationale, llm_reg_note, divert_airport = llm_result
            llm_eval = path_map.get(llm_path)
            if llm_eval is not None:
                if llm_path != math_best.path:
                    logger.info(
                        "[%s] Gemini overrides math selection: %s → %s",
                        assessment.shipment_id, math_best.path, llm_path,
                    )
                best           = llm_eval
                rationale      = llm_rationale
                regulatory_note= llm_reg_note
                gemini_decided = True
            else:
                best      = math_best
                rationale = self._build_plan_rationale(assessment, tts, delay_hours, math_best, viable_paths)
                regulatory_note = None
        else:
            best      = math_best
            rationale = self._build_plan_rationale(assessment, tts, delay_hours, math_best, viable_paths)
            regulatory_note = None

        chosen_carrier  = best.carrier
        chosen_facility = best.facility
        urgency         = self._urgency(assessment.risk_score)
        hospital_note   = self._build_hospital_eta_note(assessment, best, tts, destination)

        plan_kwargs: Dict[str, Any] = dict(
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
            cold_storage_iata      = best.iata if best.path == "COLD_STORAGE" else None,
            divert_airport_iata    = divert_airport.get("iata") if divert_airport else None,
            divert_airport_name    = divert_airport.get("name") if divert_airport else None,
            divert_airport_city    = divert_airport.get("city") if divert_airport else None,
            divert_distance_km     = divert_airport.get("distance_km") if divert_airport else None,
            urgency                = urgency,
            rationale              = rationale,
            hospital_eta_note      = hospital_note,
            gemini_decision        = gemini_decided,
        )
        if regulatory_note:
            plan_kwargs["regulatory_note"] = regulatory_note

        plan = ReroutePlan(**plan_kwargs)

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
        # Use current GPS position for nearest facility (preferred over destination name)
        metadata = getattr(assessment, "metadata", {})
        current_coords = None
        lat = metadata.get("latitude") or metadata.get("lat")
        lon = metadata.get("longitude") or metadata.get("lon")
        if lat is not None and lon is not None:
            try:
                current_coords = (float(lat), float(lon))
            except (ValueError, TypeError):
                pass

        facility_name, eta, iata = self._nearest_facility(destination, current_coords)
        margin = tts - eta
        return PathEvaluation(
            path     = "COLD_STORAGE",
            eta_hours= eta,
            viable   = eta < tts,
            margin_hours = margin,
            facility = facility_name,
            iata     = iata or None,
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
        Cap at the product's own excursion budget (not a fixed 72h).
        """
        spoilage_prob = getattr(assessment, "spoilage_prob", 0.0)
        product_id    = getattr(assessment, "metadata", {}).get("product_id", "VACC-STANDARD")

        budget = 72.0
        try:
            from data.product_catalogue import get_product_profile
            profile = get_product_profile(product_id)
            if profile:
                budget = float(profile.excursion_max_hours)
        except Exception:
            pass
        if budget <= 0:
            budget = 72.0

        remaining = max(0.0, 1.0 - spoilage_prob) * budget

        if spoilage_prob > 0.3:
            remaining /= 1.5

        return max(0.5, min(remaining, budget))

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

    def _nearest_facility(self, destination: str, current_coords: Optional[Tuple[float, float]] = None) -> Tuple[str, float, str]:
        """
        Return (facility_name, divert_hours, iata_code) for the cold-storage hub
        closest to the aircraft's current position (preferred) or destination name.

        If current_coords (lat, lon) are provided, uses GPS distance.
        Otherwise falls back to name-based matching.
        """
        # GPS-based selection (preferred — always picks the truly nearest facility)
        if current_coords is not None and _FACILITY_COORDS:
            best_iata = ""
            best_dist = float("inf")
            for iata, coords in _FACILITY_COORDS.items():
                if iata not in _COLD_STORAGE_FACILITIES:
                    continue
                dist = _haversine_km_coords(current_coords, coords)
                if dist < best_dist:
                    best_dist = dist
                    best_iata = iata
            if best_iata:
                fname, hours = _COLD_STORAGE_FACILITIES[best_iata]
                # Adjust ETA based on distance (rough: 800 km/h cruise speed)
                adjusted_hours = max(hours, best_dist / 800.0)
                return fname, round(adjusted_hours, 1), best_iata

        # Fallback: name-based matching
        dest_upper = destination.upper()
        dest_lower = destination.lower()

        # 1. Direct IATA
        if dest_upper in _COLD_STORAGE_FACILITIES:
            fname, hours = _COLD_STORAGE_FACILITIES[dest_upper]
            return fname, hours, dest_upper

        # 2. Full airport-name → IATA
        for name_key, iata in _AIRPORT_NAME_TO_IATA.items():
            if name_key in dest_lower:
                if iata in _COLD_STORAGE_FACILITIES:
                    fname, hours = _COLD_STORAGE_FACILITIES[iata]
                    return fname, hours, iata

        # 3. IATA code anywhere in destination string
        for code, (fname, hours) in _COLD_STORAGE_FACILITIES.items():
            if code in dest_upper:
                return fname, hours, code

        fname, hours = _DEFAULT_FACILITY
        return fname, hours, ""

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

    def _llm_select_path(
        self,
        assessment: Any,
        path_a: PathEvaluation,
        path_b: PathEvaluation,
        path_c: PathEvaluation,
        tts: float,
        delay_hours: float,
    ) -> Optional[Tuple[str, str, str, Optional[Dict]]]:
        """
        Ask Gemini to select the best reroute path AND recommend a specific
        divert airport when applicable.

        Gemini receives:
          - Full shipment context (product, risk, spoilage window, battery level)
          - Live weather severity at the destination
          - A list of real nearby airports with their cold-chain capability,
            current congestion level, and distance from the destination
          - The three evaluated paths with ETAs and viability margins

        Gemini returns ONE chosen path, ONE specific divert airport (IATA code),
        and a clear human-readable rationale for the human operator to approve/reject.

        Returns (chosen_path, rationale, regulatory_note, divert_airport_dict) or None.
        """
        if not GEMINI_API_KEY:
            return None

        try:
            from google import genai
            from google.genai import types

            metadata     = getattr(assessment, "metadata", {})
            carrier      = metadata.get("carrier", "Unknown")
            destination  = metadata.get("destination", "Unknown")
            battery_pct  = metadata.get("battery_pct", 100.0)
            phase        = metadata.get("phase", "UNKNOWN")
            weather_sev  = float(metadata.get("weather_severity", 0.0))
            dest_lat     = metadata.get("latitude")
            dest_lon     = metadata.get("longitude")

            # Find nearby airports around the current position
            nearby: List[Dict] = []
            if dest_lat is not None and dest_lon is not None:
                nearby = _airports_near(float(dest_lat), float(dest_lon),
                                        radius_km=800, max_results=6)

            # Build the nearby airports block for the prompt
            if nearby:
                nearby_lines = "\n".join(
                    f"  {i+1}. {a['iata']} — {a['name']}, {a['city']} "
                    f"({a['distance_km']:.0f} km away | "
                    f"cold-chain: {a['cold_chain']} | "
                    f"congestion: {a['congestion']})"
                    for i, a in enumerate(nearby)
                )
            else:
                nearby_lines = "  (position unavailable — use domain knowledge for nearest hub)"

            # Map congestion to weather estimate near destination
            weather_desc = (
                "SEVERE STORM — airport closed / holding mandatory"
                if weather_sev >= 0.8 else
                "HEAVY WEATHER — delays likely, ILS approaches only"
                if weather_sev >= 0.6 else
                "MODERATE WEATHER — reduced capacity"
                if weather_sev >= 0.4 else
                "LIGHT WEATHER / CLEAR"
            )

            battery_status = (
                "CRITICAL (<10%) — cooling unit may fail imminently"
                if battery_pct < 10 else
                f"LOW ({battery_pct:.0f}%) — cold-chain integrity at risk"
                if battery_pct < 20 else
                f"REDUCED ({battery_pct:.0f}%) — monitor closely"
                if battery_pct < 40 else
                f"OK ({battery_pct:.0f}%)"
            )

            anomaly_lines = "\n".join(
                f"  - [{a.severity.value}] {a.anomaly_type.value}: {a.description}"
                for a in getattr(assessment, "anomalies", [])
            ) or "  - None"

            nearby_iata_list = [a["iata"] for a in nearby]

            prompt = f"""You are the AI co-pilot for a pharmaceutical cold-chain emergency response system.
A cargo shipment is in crisis. You must recommend the SINGLE BEST course of action to a human operator
who will approve or reject your decision on the HITL dashboard.

━━━ SHIPMENT STATUS ━━━
Shipment ID      : {assessment.shipment_id}
Product          : {metadata.get("product_id", "VACC-STANDARD")}
Current Phase    : {phase}  (cargo is {"inside the aircraft" if phase in ("ENROUTE","HOLDING","WAIT_TAKEOFF") else "on the ground"})
Risk Level       : {assessment.risk_level.value}  (score {assessment.risk_score:.2f}/1.00)
Spoilage Prob    : {assessment.spoilage_prob:.0%}
Time to Spoilage : {tts:.1f} hours remaining before product becomes unviable
Current Delay    : {delay_hours:.1f} hours
Current Carrier  : {carrier}
Intended Dest    : {destination}

━━━ CRITICAL CONDITIONS ━━━
Battery Status   : {battery_status}
  → Battery powers the container's active cooling unit.
  → If battery dies mid-flight, cold chain collapses regardless of aircraft environment.
Destination Wx   : {weather_desc}  (severity index {weather_sev:.2f}/1.00)

Active Anomalies :
{anomaly_lines}

━━━ NEARBY AIRPORTS (within 800 km of current position) ━━━
{nearby_lines}

Note on congestion: HIGH congestion airports have longer ground handling times (+1-2h).
Note on cold-chain: FULL = IATA CEIV Pharma certified; PARTIAL = refrigerated handling only.
When destination is STORM/closed, aircraft CANNOT land — it must divert.

━━━ THREE OPTIONS EVALUATED ━━━
PATH A — COLD_STORAGE (divert to nearest pharma-certified airport)
  ETA      : {path_a.eta_hours:.1f}h  |  Viable: {path_a.viable}  |  Margin: {path_a.margin_hours:+.1f}h
  Details  : {path_a.notes}

PATH B — LAST_MILE_COURIER (transfer cargo to emergency pharma courier at nearest safe airport)
  ETA      : {path_b.eta_hours:.1f}h  |  Viable: {path_b.viable}  |  Margin: {path_b.margin_hours:+.1f}h
  Carrier  : {path_b.carrier or "N/A"}
  Details  : {path_b.notes}

PATH C — ORIGINAL_ROUTE (continue to intended destination, absorb delay)
  ETA      : {path_c.eta_hours:.1f}h  |  Viable: {path_c.viable}  |  Margin: {path_c.margin_hours:+.1f}h
  Details  : {path_c.notes}

━━━ YOUR TASK ━━━
1. Select the BEST path weighing: battery time remaining, weather at each nearby airport,
   cold-chain capability, congestion, and product spoilage window.
2. If choosing COLD_STORAGE or LAST_MILE_COURIER, pick the SPECIFIC best divert airport
   from the nearby list (or use your knowledge if the list is empty).
3. Write 3-4 sentences of expert reasoning that a human logistics director can read,
   understand, and approve in under 30 seconds. Be specific: cite the battery %, spoilage
   window, weather severity, and why THIS airport beats the alternatives.
4. Cite the GDP/FDA regulation that mandates this action.

Respond ONLY with valid JSON — no markdown, no extra text:
{{
  "chosen_path": "COLD_STORAGE",
  "divert_airport_iata": "SIN",
  "rationale": "3-4 sentences citing battery %, time-to-spoilage, weather severity, and why this specific airport was chosen over alternatives.",
  "regulatory_note": "The specific GDP/FDA/ICH Q10 regulation requiring this action."
}}

Rules:
- chosen_path MUST be exactly one of: COLD_STORAGE, LAST_MILE_COURIER, ORIGINAL_ROUTE
- divert_airport_iata: use an IATA code from the nearby list if available, or best known hub; set to null if ORIGINAL_ROUTE
- If no path is viable, choose the least-bad one and note quarantine must follow"""

            client   = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model   = LLM_MODEL,
                contents= prompt,
                config  = types.GenerateContentConfig(
                    temperature      = 0.2,   # low temp for consistent operational decisions
                    max_output_tokens= 600,
                ),
            )

            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data      = json.loads(raw)
            chosen    = data.get("chosen_path", "").strip().upper()
            rationale = data.get("rationale", "").strip()
            reg_note  = data.get("regulatory_note", "").strip()
            div_iata  = (data.get("divert_airport_iata") or "").strip().upper() or None

            valid_paths = {"COLD_STORAGE", "LAST_MILE_COURIER", "ORIGINAL_ROUTE"}
            if chosen not in valid_paths:
                logger.warning("[%s] Gemini invalid path '%s' — math fallback",
                               assessment.shipment_id, chosen)
                return None

            # Resolve divert airport from our DB or nearby list
            divert_info: Optional[Dict] = None
            if div_iata:
                if div_iata in _AIRPORT_DB:
                    db_entry = _AIRPORT_DB[div_iata]
                    # compute distance if we have position
                    dist = None
                    if dest_lat is not None and dest_lon is not None:
                        dist = round(_haversine_km(float(dest_lat), float(dest_lon),
                                                   db_entry["lat"], db_entry["lon"]), 0)
                    divert_info = {
                        "iata":        div_iata,
                        "name":        db_entry["name"],
                        "city":        db_entry["city"],
                        "distance_km": dist,
                    }
                else:
                    # Gemini used an IATA not in our DB — still pass it through
                    divert_info = {"iata": div_iata, "name": div_iata, "city": "", "distance_km": None}

            logger.info(
                "[%s] Gemini reroute: path=%s  divert=%s  (math had: %s)",
                assessment.shipment_id, chosen, div_iata or "N/A",
                "see plan_reroute for math winner",
            )
            return chosen, rationale, reg_note, divert_info

        except Exception as exc:
            logger.warning("[%s] Gemini reroute failed (%s) — math fallback",
                           assessment.shipment_id, exc)
            return None

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
