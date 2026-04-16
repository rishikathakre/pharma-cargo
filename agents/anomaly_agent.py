"""
AnomalyAgent
------------
Analyses a TelemetryRecord (and its rolling history) to detect sensor
anomalies.  Returns a list of Anomaly objects describing what was found.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from agents.telemetry_agent import TelemetryAgent, TelemetryRecord
from config import (
    HUMIDITY_MAX_PCT,
    SHOCK_MAX_G,
    TEMP_MAX_C,
    TEMP_MIN_C,
    EXCURSION_MINUTES,
)
from data.product_catalogue import get_product_profile, get_default_product_id

logger = logging.getLogger(__name__)


class AnomalyType(str, Enum):
    TEMP_HIGH        = "TEMPERATURE_EXCURSION_HIGH"
    TEMP_LOW         = "TEMPERATURE_EXCURSION_LOW"
    HUMIDITY_HIGH    = "HUMIDITY_EXCURSION"
    SHOCK_EVENT      = "SHOCK_EVENT"
    CUSTOMS_HOLD     = "CUSTOMS_HOLD"
    FLIGHT_DELAY     = "FLIGHT_DELAY"
    FLIGHT_DIVERSION = "FLIGHT_DIVERSION"
    BATTERY_LOW      = "BATTERY_LOW"
    SUSTAINED_EXCURSION = "SUSTAINED_TEMP_EXCURSION"
    SEVERE_WEATHER   = "SEVERE_WEATHER"


class Severity(str, Enum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class Anomaly:
    anomaly_type:  AnomalyType
    severity:      Severity
    shipment_id:   str
    container_id:  str
    detected_at:   datetime
    description:   str
    measured_value: Optional[float] = None
    threshold:      Optional[float] = None
    duration_min:   Optional[float] = None
    metadata:       dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "anomaly_type":   self.anomaly_type.value,
            "severity":       self.severity.value,
            "shipment_id":    self.shipment_id,
            "container_id":   self.container_id,
            "detected_at":    self.detected_at.isoformat(),
            "description":    self.description,
            "measured_value": self.measured_value,
            "threshold":      self.threshold,
            "duration_min":   self.duration_min,
        }


class AnomalyAgent:
    """
    Rule-based + statistical anomaly detection.
    Extend _ml_check() later for model-based detection.
    """

    def __init__(self, telemetry_agent: TelemetryAgent):
        self._tel = telemetry_agent

    def analyse(self, record: TelemetryRecord) -> List[Anomaly]:
        anomalies: List[Anomaly] = []
        now = datetime.now(timezone.utc)

        product_id = (record.raw or {}).get("product_id") or get_default_product_id()
        profile = get_product_profile(product_id)
        # Fall back to global config thresholds if catalogue is missing.
        temp_min = profile.temp_min_c if profile else TEMP_MIN_C
        temp_max = profile.temp_max_c if profile else TEMP_MAX_C

        # --- Temperature excursion ---
        phase = (record.raw or {}).get("phase", "")
        phase_note = " [in-flight — risk weight reduced]" if phase == "ENROUTE" else ""

        if record.temperature_c > temp_max:
            sev = Severity.CRITICAL if record.temperature_c > temp_max + 4 else Severity.HIGH
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.TEMP_HIGH,
                severity       = sev,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = (f"Temperature {record.temperature_c:.1f}°C exceeds "
                                  f"upper limit {temp_max}°C{phase_note}"),
                measured_value = record.temperature_c,
                threshold      = temp_max,
            ))

        if record.temperature_c < temp_min:
            sev = Severity.CRITICAL if record.temperature_c < temp_min - 4 else Severity.HIGH
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.TEMP_LOW,
                severity       = sev,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = (f"Temperature {record.temperature_c:.1f}°C below "
                                  f"lower limit {temp_min}°C{phase_note}"),
                measured_value = record.temperature_c,
                threshold      = temp_min,
            ))

        # --- Sustained temperature excursion ---
        duration = self._excursion_duration_minutes(record)
        if duration and duration >= EXCURSION_MINUTES:
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.SUSTAINED_EXCURSION,
                severity       = Severity.CRITICAL,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = (f"Continuous temperature excursion for "
                                  f"{duration:.0f} min (threshold {EXCURSION_MINUTES} min)"),
                duration_min   = duration,
            ))

        # --- Humidity ---
        if record.humidity_pct > HUMIDITY_MAX_PCT:
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.HUMIDITY_HIGH,
                severity       = Severity.MEDIUM,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = (f"Humidity {record.humidity_pct:.1f}% exceeds "
                                  f"limit {HUMIDITY_MAX_PCT}%"),
                measured_value = record.humidity_pct,
                threshold      = HUMIDITY_MAX_PCT,
            ))

        # --- Shock ---
        if record.shock_g > SHOCK_MAX_G:
            sev = Severity.HIGH if record.shock_g > SHOCK_MAX_G * 2 else Severity.MEDIUM
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.SHOCK_EVENT,
                severity       = sev,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = (f"Shock {record.shock_g:.2f}g exceeds limit {SHOCK_MAX_G}g"),
                measured_value = record.shock_g,
                threshold      = SHOCK_MAX_G,
            ))

        # --- Customs hold ---
        if record.customs_status == "HOLD":
            anomalies.append(Anomaly(
                anomaly_type = AnomalyType.CUSTOMS_HOLD,
                severity     = Severity.HIGH,
                shipment_id  = record.shipment_id,
                container_id = record.container_id,
                detected_at  = now,
                description  = "Shipment placed on customs HOLD",
            ))

        # --- Flight status ---
        if record.flight_status == "DELAYED":
            sev = Severity.HIGH if record.delay_hours > 2 else Severity.MEDIUM
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.FLIGHT_DELAY,
                severity       = sev,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = f"Flight delayed by {record.delay_hours:.1f} hours",
                measured_value = record.delay_hours,
            ))

        if record.flight_status == "DIVERTED":
            anomalies.append(Anomaly(
                anomaly_type = AnomalyType.FLIGHT_DIVERSION,
                severity     = Severity.CRITICAL,
                shipment_id  = record.shipment_id,
                container_id = record.container_id,
                detected_at  = now,
                description  = "Flight diverted — destination changed",
            ))

        # --- Battery / cooling unit ---
        # The battery powers the container's active cooling unit.
        # Graduated severity so operators get early warning before cooling fails.
        batt = record.battery_pct
        if batt < 10:
            batt_sev = Severity.CRITICAL
            batt_msg = (f"Cooling unit battery CRITICAL at {batt:.0f}% — "
                        f"active refrigeration may fail imminently")
        elif batt < 20:
            batt_sev = Severity.HIGH
            batt_msg = (f"Cooling unit battery LOW at {batt:.0f}% — "
                        f"cold-chain integrity at risk")
        elif batt < 40:
            batt_sev = Severity.MEDIUM
            batt_msg = (f"Cooling unit battery at {batt:.0f}% — "
                        f"monitor closely; recharge at next opportunity")
        elif batt < 60:
            batt_sev = Severity.LOW
            batt_msg = f"Cooling unit battery at {batt:.0f}% — early advisory"
        else:
            batt_sev = None

        if batt_sev is not None:
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.BATTERY_LOW,
                severity       = batt_sev,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = batt_msg,
                measured_value = batt,
            ))

        # --- Severe weather ---
        ws = record.weather_severity
        if ws >= 0.8:
            w_sev = Severity.CRITICAL
            w_msg = f"Extreme weather (severity {ws:.2f}) — storm conditions threaten cold-chain and flight safety"
        elif ws >= 0.6:
            w_sev = Severity.HIGH
            w_msg = f"Severe weather (severity {ws:.2f}) — significant risk to schedule and cargo integrity"
        elif ws >= 0.4:
            w_sev = Severity.MEDIUM
            w_msg = f"Adverse weather (severity {ws:.2f}) — potential delays and handling disruption"
        else:
            w_sev = None

        if w_sev is not None:
            anomalies.append(Anomaly(
                anomaly_type   = AnomalyType.SEVERE_WEATHER,
                severity       = w_sev,
                shipment_id    = record.shipment_id,
                container_id   = record.container_id,
                detected_at    = now,
                description    = w_msg,
                measured_value = ws,
                threshold      = 0.4,
            ))

        if anomalies:
            logger.info("[%s] %d anomalies detected: %s",
                        record.shipment_id, len(anomalies),
                        [a.anomaly_type.value for a in anomalies])
        return anomalies

    # ------------------------------------------------------------------
    def _excursion_duration_minutes(self, record: TelemetryRecord) -> Optional[float]:
        """Return continuous excursion duration from history (minutes)."""
        history = self._tel.get_history(record.shipment_id)
        if len(history) < 2:
            return None

        product_id = (record.raw or {}).get("product_id") or get_default_product_id()
        profile = get_product_profile(product_id)
        temp_min = profile.temp_min_c if profile else TEMP_MIN_C
        temp_max = profile.temp_max_c if profile else TEMP_MAX_C

        duration = 0.0
        for i in range(len(history) - 1, 0, -1):
            r = history[i]
            if not (temp_min <= r.temperature_c <= temp_max):
                prev = history[i - 1]
                delta = (r.timestamp - prev.timestamp).total_seconds() / 60.0
                duration += max(delta, 0)
            else:
                break
        return duration if duration > 0 else None
