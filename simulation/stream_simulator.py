"""
StreamSimulator
---------------
Generates realistic synthetic telemetry for multiple concurrent shipments.
Simulates:
  - Normal cold-chain readings
  - Gradual temperature drift (refrigeration failure)
  - Sudden shock events
  - Customs holds
  - Flight delays / diversions
  - Battery drain

Usage:
    sim = StreamSimulator()
    for payload in sim.stream(max_ticks=100):
        orchestrator.run(payload)
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Generator, List, Optional

from config import (
    SIMULATION_INTERVAL_SEC,
    SIMULATION_SHIPMENTS,
    TEMP_MAX_C,
    TEMP_MIN_C,
)

try:
    from data.dataset_loader import loader as _dataset_loader
    _CARRIER_PROFILES = _dataset_loader.carrier_profiles
    _DELAYED_ROUTES   = _dataset_loader.delayed_routes
    _ALL_ROUTES       = _dataset_loader.shipment_routes
except Exception:
    _CARRIER_PROFILES = {}
    _DELAYED_ROUTES   = []
    _ALL_ROUTES       = []


# ---------------------------------------------------------------------------
# Shipment scenario profiles
# ---------------------------------------------------------------------------

SCENARIOS = [
    "normal",
    "temp_excursion_high",
    "temp_excursion_low",
    "customs_hold",
    "flight_delay",
    "flight_diversion",
    "shock_event",
    "battery_drain",
    "sustained_excursion",
]


class ShipmentState:
    def __init__(
        self,
        shipment_id:  str,
        container_id: str,
        scenario:     str,
        origin:       str = "JFK",
        destination:  str = "FRA",
        carrier:      str = "GlobalConnect",
        carrier_delay_baseline: float = 2.0,
        customs_days: float = 2.0,
    ):
        self.shipment_id  = shipment_id
        self.container_id = container_id
        self.scenario     = scenario
        self.tick         = 0

        # Route metadata (from real shipment data)
        self.origin      = origin
        self.destination = destination
        self.carrier     = carrier
        self.carrier_delay_baseline = carrier_delay_baseline
        self.customs_days = customs_days

        # Location (JFK baseline — lon/lat updated per tick)
        self.latitude    = 40.6413 + random.uniform(-1, 1)
        self.longitude   = -73.7781 + random.uniform(-2, 2)
        self.altitude_m  = 10000.0

        # Sensor state
        self.temperature_c  = random.uniform(TEMP_MIN_C + 0.5, TEMP_MAX_C - 0.5)
        self.humidity_pct   = random.uniform(40.0, 65.0)
        self.shock_g        = 0.0
        self.battery_pct    = 100.0
        self.customs_status = "CLEARED"
        self.flight_status  = "ON_TIME"
        self.delay_hours    = 0.0
        self.planned_eta    = datetime.now(timezone.utc) + timedelta(hours=18)

    def advance(self) -> Dict[str, Any]:
        self.tick += 1
        self._apply_scenario()
        self._add_noise()
        self._drain_battery()
        self._move_location()

        return {
            "shipment_id":    self.shipment_id,
            "container_id":   self.container_id,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "temperature_c":  round(self.temperature_c, 2),
            "humidity_pct":   round(self.humidity_pct, 1),
            "shock_g":        round(self.shock_g, 3),
            "latitude":       round(self.latitude, 6),
            "longitude":      round(self.longitude, 6),
            "altitude_m":     round(self.altitude_m, 1),
            "customs_status": self.customs_status,
            "flight_status":  self.flight_status,
            "delay_hours":    round(self.delay_hours, 2),
            "battery_pct":    round(self.battery_pct, 1),
            # Route context from real dataset
            "carrier":        self.carrier,
            "origin":         self.origin,
            "destination":    self.destination,
        }

    # ------------------------------------------------------------------

    def _apply_scenario(self) -> None:
        if self.scenario == "normal":
            # Stable cold-chain
            target = (TEMP_MIN_C + TEMP_MAX_C) / 2
            self.temperature_c += (target - self.temperature_c) * 0.05

        elif self.scenario == "temp_excursion_high":
            if self.tick > 5:
                self.temperature_c += 0.8   # refrigeration failure drift
                self.humidity_pct  += 0.3

        elif self.scenario == "temp_excursion_low":
            if self.tick > 5:
                self.temperature_c -= 0.6   # over-cooling

        elif self.scenario == "sustained_excursion":
            if self.tick > 3:
                self.temperature_c = TEMP_MAX_C + 2.0 + (self.tick * 0.1)

        elif self.scenario == "customs_hold":
            if self.tick == 4:
                self.customs_status = "HOLD"
                self.delay_hours    = 8.0
                self.flight_status  = "DELAYED"

        elif self.scenario == "flight_delay":
            if self.tick == 3:
                self.flight_status = "DELAYED"
                # Seed delay from real carrier baseline (worse carriers start higher)
                self.delay_hours   = max(6.0, self.carrier_delay_baseline * 2.5)
            elif self.tick > 3:
                self.delay_hours  += 0.5

        elif self.scenario == "flight_diversion":
            if self.tick == 5:
                self.flight_status  = "DIVERTED"
                self.delay_hours    = 14.0

        elif self.scenario == "shock_event":
            self.shock_g = 8.5 if self.tick == 3 else 0.0

        elif self.scenario == "battery_drain":
            self.battery_pct = max(0, 100 - self.tick * 8)

    def _add_noise(self) -> None:
        self.temperature_c += random.gauss(0, 0.1)
        self.humidity_pct  += random.gauss(0, 0.5)
        self.humidity_pct   = max(0, min(100, self.humidity_pct))
        if self.scenario != "shock_event":
            self.shock_g = max(0, random.gauss(0.2, 0.05))

    def _drain_battery(self) -> None:
        self.battery_pct = max(0, self.battery_pct - random.uniform(0.05, 0.2))

    def _move_location(self) -> None:
        # Simulate eastward flight path
        self.longitude += random.uniform(0.3, 0.6)
        self.latitude  += random.gauss(0, 0.05)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class StreamSimulator:
    """
    Produces synthetic telemetry payloads for multiple concurrent shipments.
    """

    def __init__(
        self,
        n_shipments: int          = SIMULATION_SHIPMENTS,
        interval_sec: float       = SIMULATION_INTERVAL_SEC,
        scenarios: Optional[List[str]] = None,
    ):
        self._interval = interval_sec
        self._shipments: List[ShipmentState] = []

        picked   = scenarios or random.choices(SCENARIOS, k=n_shipments)
        carriers = list(_CARRIER_PROFILES.values()) if _CARRIER_PROFILES else []
        routes   = _ALL_ROUTES if _ALL_ROUTES else []

        for i, scenario in enumerate(picked):
            # Pick a real route if available
            route = random.choice(routes) if routes else None
            # Pick carrier: delayed scenarios use worse carriers for realism
            if carriers:
                if scenario in ("flight_delay", "flight_diversion", "customs_hold"):
                    # Weight towards less reliable carriers for these scenarios
                    weights = [max(0.01, 1.0 - c.reliability_score) for c in carriers]
                else:
                    weights = [c.reliability_score for c in carriers]
                carrier = random.choices(carriers, weights=weights, k=1)[0]
            else:
                carrier = None

            self._shipments.append(ShipmentState(
                shipment_id             = f"SHP-{1000 + i:04d}",
                container_id            = f"CNT-{2000 + i:04d}",
                scenario                = scenario,
                origin                  = route.origin      if route   else "JFK",
                destination             = route.destination if route   else "FRA",
                carrier                 = carrier.name      if carrier else "GlobalConnect",
                carrier_delay_baseline  = carrier.avg_delay_hours if carrier else 2.0,
                customs_days            = route.customs_clearance_days if route else 2.0,
            ))

    def stream(
        self,
        max_ticks: Optional[int] = None,
        realtime: bool = True,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Yield telemetry dicts indefinitely (or up to max_ticks per shipment).
        Set realtime=False for tests (no sleep).
        """
        tick = 0
        while max_ticks is None or tick < max_ticks:
            for shipment in self._shipments:
                payload = shipment.advance()
                yield payload
            tick += 1
            if realtime:
                time.sleep(self._interval)

    def single_tick(self) -> List[Dict[str, Any]]:
        """Advance all shipments one tick and return payloads."""
        return [s.advance() for s in self._shipments]

    def get_shipment_ids(self) -> List[str]:
        return [s.shipment_id for s in self._shipments]
