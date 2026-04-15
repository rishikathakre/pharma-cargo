"""
TelemetryAgent
--------------
Ingests raw IoT telemetry from smart containers and normalises it into a
structured TelemetryRecord.  In production this would subscribe to an MQTT /
Kafka topic; here it accepts a dict or simulated payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TelemetryRecord:
    """Normalised snapshot from a single container sensor read."""
    shipment_id:      str
    container_id:     str
    timestamp:        datetime
    temperature_c:    float
    humidity_pct:     float
    shock_g:          float
    latitude:         float
    longitude:        float
    altitude_m:       float
    customs_status:   str                        # e.g. "CLEARED", "HOLD", "PENDING"
    flight_status:    str                        # e.g. "ON_TIME", "DELAYED", "DIVERTED"
    delay_hours:      float                      # estimated delay vs. planned ETA
    battery_pct:      float
    raw:              Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shipment_id":    self.shipment_id,
            "container_id":   self.container_id,
            "timestamp":      self.timestamp.isoformat(),
            "temperature_c":  self.temperature_c,
            "humidity_pct":   self.humidity_pct,
            "shock_g":        self.shock_g,
            "latitude":       self.latitude,
            "longitude":      self.longitude,
            "altitude_m":     self.altitude_m,
            "customs_status": self.customs_status,
            "flight_status":  self.flight_status,
            "delay_hours":    self.delay_hours,
            "battery_pct":    self.battery_pct,
        }


class TelemetryAgent:
    """
    Responsible for:
    1. Receiving raw telemetry payloads.
    2. Validating & normalising fields.
    3. Maintaining a rolling history per shipment.
    4. Returning a TelemetryRecord for downstream agents.
    """

    def __init__(self, history_size: int = 100):
        self._history: Dict[str, List[TelemetryRecord]] = {}
        self._history_size = history_size

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ingest(self, raw_payload: Dict[str, Any]) -> TelemetryRecord:
        """Parse a raw dict payload → TelemetryRecord."""
        record = self._parse(raw_payload)
        self._store(record)
        logger.debug("Ingested telemetry for %s @ %s", record.shipment_id, record.timestamp)
        return record

    def get_history(self, shipment_id: str) -> List[TelemetryRecord]:
        return list(self._history.get(shipment_id, []))

    def latest(self, shipment_id: str) -> Optional[TelemetryRecord]:
        history = self._history.get(shipment_id)
        return history[-1] if history else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse(self, payload: Dict[str, Any]) -> TelemetryRecord:
        ts_raw = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            ts = datetime.now(timezone.utc)

        return TelemetryRecord(
            shipment_id    = str(payload.get("shipment_id", "UNKNOWN")),
            container_id   = str(payload.get("container_id", "UNKNOWN")),
            timestamp      = ts,
            temperature_c  = float(payload.get("temperature_c", 0.0)),
            humidity_pct   = float(payload.get("humidity_pct", 0.0)),
            shock_g        = float(payload.get("shock_g", 0.0)),
            latitude       = float(payload.get("latitude", 0.0)),
            longitude      = float(payload.get("longitude", 0.0)),
            altitude_m     = float(payload.get("altitude_m", 0.0)),
            customs_status = str(payload.get("customs_status", "UNKNOWN")),
            flight_status  = str(payload.get("flight_status", "UNKNOWN")),
            delay_hours    = float(payload.get("delay_hours", 0.0)),
            battery_pct    = float(payload.get("battery_pct", 100.0)),
            raw            = payload,
        )

    def _store(self, record: TelemetryRecord) -> None:
        bucket = self._history.setdefault(record.shipment_id, [])
        bucket.append(record)
        if len(bucket) > self._history_size:
            bucket.pop(0)
