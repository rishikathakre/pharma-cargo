"""
DatasetLoader
-------------
Loads and analyses the raw CSV datasets to derive:
  - Carrier reliability profiles (from logistics_performance.csv)
  - Risk calibration thresholds (from supply_chain_risk_dataset.csv)
  - Real-world shipment routes (from shipment.csv)
  - Vaccination demand by state (from us_state_vaccinations.csv)

Used by:
  simulation/stream_simulator.py  → realistic carrier delays + real routes
  notifications/hospital_notifier.py → vaccination-demand-based priority
  main.py                          → calibration summary at startup
"""

from __future__ import annotations

import csv
import logging
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "raw"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CarrierProfile:
    name:              str
    avg_delay_hours:   float
    damage_claims:     int
    reliability_score: float   # 0-1, higher = better


@dataclass
class RiskCalibration:
    """Thresholds derived from supply_chain_risk_dataset.csv."""
    shock_warning_g:    float   # avg vibration at risk_label=1
    shock_critical_g:   float   # avg vibration at risk_label=2
    humidity_warning_pct: float # avg humidity at risk_label=1
    at_risk_ratio:      float   # fraction of historical records labelled at-risk
    delay_prone_ratio:  float   # fraction of delayed shipments in supply chain data


@dataclass
class ShipmentRoute:
    origin:                    str
    destination:               str
    customs_clearance_days:    float
    delayed:                   bool


@dataclass
class VaccinationDemand:
    location:           str
    daily_vaccinations: float
    priority_tier:      str     # "CRITICAL" | "HIGH" | "STANDARD"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class DatasetLoader:
    """
    Loads all raw datasets once at import time.
    Access via the module-level singleton `loader`.
    """

    def __init__(self):
        self.carrier_profiles:    Dict[str, CarrierProfile]  = {}
        self.risk_calibration:    Optional[RiskCalibration]  = None
        self.shipment_routes:     List[ShipmentRoute]        = []
        self.delayed_routes:      List[ShipmentRoute]        = []
        self.vaccination_demand:  Dict[str, VaccinationDemand] = {}
        self._loaded = False

    def load(self) -> "DatasetLoader":
        if self._loaded:
            return self
        try:
            self._load_logistics_performance()
        except Exception as e:
            logger.warning("Could not load logistics_performance.csv: %s", e)
        try:
            self._load_supply_chain_risk()
        except Exception as e:
            logger.warning("Could not load supply_chain_risk_dataset.csv: %s", e)
        try:
            self._load_shipments()
        except Exception as e:
            logger.warning("Could not load shipment.csv: %s", e)
        try:
            self._load_vaccinations()
        except Exception as e:
            logger.warning("Could not load us_state_vaccinations.csv: %s", e)

        self._loaded = True
        logger.info("DatasetLoader: loaded %d carriers, %d routes (%d delayed), "
                    "%d vaccination locations",
                    len(self.carrier_profiles), len(self.shipment_routes),
                    len(self.delayed_routes), len(self.vaccination_demand))
        return self

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_best_carriers(self, n: int = 2) -> List[CarrierProfile]:
        """Return the n most reliable carriers (for rerouting suggestions)."""
        ranked = sorted(self.carrier_profiles.values(),
                        key=lambda c: c.reliability_score, reverse=True)
        return ranked[:n]

    def get_worst_carriers(self, n: int = 2) -> List[CarrierProfile]:
        """Return the n least reliable carriers (for risk context)."""
        ranked = sorted(self.carrier_profiles.values(),
                        key=lambda c: c.reliability_score)
        return ranked[:n]

    def get_hospital_priority(self, state_or_location: str) -> str:
        """Return priority tier for a state based on vaccination demand."""
        demand = self.vaccination_demand.get(state_or_location)
        return demand.priority_tier if demand else "STANDARD"

    def get_high_priority_locations(self) -> List[str]:
        """Return locations with CRITICAL or HIGH vaccination priority."""
        return [
            loc for loc, d in self.vaccination_demand.items()
            if d.priority_tier in ("CRITICAL", "HIGH")
        ]

    def calibration_summary(self) -> str:
        lines = ["=== Dataset Calibration Summary ==="]

        if self.carrier_profiles:
            lines.append("Carrier reliability (from logistics_performance.csv):")
            for c in sorted(self.carrier_profiles.values(),
                            key=lambda x: x.reliability_score, reverse=True):
                lines.append(
                    f"  {c.name:<22} score={c.reliability_score:.2f}  "
                    f"avg_delay={c.avg_delay_hours:.2f}h  "
                    f"damage_claims={c.damage_claims}"
                )

        if self.risk_calibration:
            rc = self.risk_calibration
            lines.append("Risk calibration (from supply_chain_risk_dataset.csv):")
            lines.append(f"  Shock warning threshold : {rc.shock_warning_g:.2f}g")
            lines.append(f"  Shock critical threshold: {rc.shock_critical_g:.2f}g")
            lines.append(f"  Humidity warning        : {rc.humidity_warning_pct:.1f}%")
            lines.append(f"  Historical at-risk ratio: {rc.at_risk_ratio:.1%}")
            lines.append(f"  Historical delay ratio  : {rc.delay_prone_ratio:.1%}")

        if self.shipment_routes:
            lines.append(f"Shipment routes loaded    : {len(self.shipment_routes)} "
                         f"({len(self.delayed_routes)} delayed)")

        high_pri = self.get_high_priority_locations()
        if high_pri:
            lines.append(f"High-priority vax locations: {', '.join(high_pri[:5])}"
                         + (f" (+{len(high_pri)-5} more)" if len(high_pri) > 5 else ""))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private loaders
    # ------------------------------------------------------------------

    def _load_logistics_performance(self) -> None:
        path = _DATA_DIR / "logistics_performance.csv"
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        # Aggregate per carrier
        carrier_data: Dict[str, Dict] = {}
        for r in rows:
            name = r["carrier"]
            cd = carrier_data.setdefault(name, {"delays": [], "claims": 0})
            cd["delays"].append(float(r["delay_hours_avg"]))
            cd["claims"] += int(r["damage_claims_count"])

        # Compute reliability score: normalise delay + claims (lower = better → invert)
        all_delays = [statistics.mean(d["delays"]) for d in carrier_data.values()]
        all_claims = [d["claims"] for d in carrier_data.values()]
        max_delay  = max(all_delays) or 1.0
        max_claims = max(all_claims) or 1.0

        for name, cd in carrier_data.items():
            avg_delay = statistics.mean(cd["delays"])
            norm_delay  = avg_delay  / max_delay
            norm_claims = cd["claims"] / max_claims
            # 60% weight on delay, 40% on damage claims
            score = 1.0 - (0.6 * norm_delay + 0.4 * norm_claims)
            self.carrier_profiles[name] = CarrierProfile(
                name              = name,
                avg_delay_hours   = round(avg_delay, 2),
                damage_claims     = cd["claims"],
                reliability_score = round(score, 3),
            )

    def _load_supply_chain_risk(self) -> None:
        path = _DATA_DIR / "supply_chain_risk_dataset.csv"
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        total = len(rows)
        at_risk  = sum(1 for r in rows if r["manual_risk_label"] in ("1", "2"))
        delayed  = sum(1 for r in rows if r["shipment_status"] == "delayed")

        # Vibration (≈ shock_g) averages per risk label
        label1 = [float(r["vibration_level"]) for r in rows if r["manual_risk_label"] == "1"]
        label2 = [float(r["vibration_level"]) for r in rows if r["manual_risk_label"] == "2"]
        label1_hum = [float(r["humidity"])    for r in rows if r["manual_risk_label"] == "1"]

        self.risk_calibration = RiskCalibration(
            shock_warning_g      = round(statistics.mean(label1), 2) if label1 else 2.0,
            shock_critical_g     = round(statistics.mean(label2), 2) if label2 else 3.5,
            humidity_warning_pct = round(statistics.mean(label1_hum), 1) if label1_hum else 60.0,
            at_risk_ratio        = round(at_risk / total, 3),
            delay_prone_ratio    = round(delayed / total, 3),
        )

    def _load_shipments(self) -> None:
        path = _DATA_DIR / "shipment.csv"
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))

        for r in rows:
            try:
                route = ShipmentRoute(
                    origin                 = r["origin"].strip(),
                    destination            = r["destination"].strip(),
                    customs_clearance_days = float(r["customs_clearance_time_days"] or 0),
                    delayed                = r["delivery_status"] == "Delayed",
                )
                self.shipment_routes.append(route)
                if route.delayed:
                    self.delayed_routes.append(route)
            except (KeyError, ValueError):
                continue

    def _load_vaccinations(self) -> None:
        path = _DATA_DIR / "us_state_vaccinations.csv"
        latest: Dict[str, dict] = {}

        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                loc = row["location"]
                if row["daily_vaccinations"] and row["date"] > latest.get(loc, {}).get("date", ""):
                    latest[loc] = row

        # Compute priority tiers based on daily vaccination volume
        demand_vals = [float(r["daily_vaccinations"]) for r in latest.values()
                       if r["daily_vaccinations"]]
        if not demand_vals:
            return

        high_cutoff     = statistics.median(demand_vals) * 3.0
        moderate_cutoff = statistics.median(demand_vals) * 1.5

        for loc, r in latest.items():
            dv = float(r["daily_vaccinations"] or 0)
            if dv >= high_cutoff:
                tier = "CRITICAL"
            elif dv >= moderate_cutoff:
                tier = "HIGH"
            else:
                tier = "STANDARD"

            self.vaccination_demand[loc] = VaccinationDemand(
                location           = loc,
                daily_vaccinations = dv,
                priority_tier      = tier,
            )


# ---------------------------------------------------------------------------
# Module-level singleton — load once on first import
# ---------------------------------------------------------------------------

loader = DatasetLoader().load()
