"""
RiskAgent
---------
Aggregates anomalies into a composite RiskAssessment score (0-1) and
recommends a prioritised list of actions.  Uses a weighted scoring model
and optionally calls an LLM for natural-language justification.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from agents.anomaly_agent import Anomaly, AnomalyType, Severity
from agents.telemetry_agent import TelemetryAgent, TelemetryRecord
from config import (
    GEMINI_API_KEY,
    LLM_MODEL,
    LLM_TEMPERATURE,
    RISK_HIGH_THRESHOLD,
    RISK_MEDIUM_THRESHOLD,
    RISK_WEIGHTS,
    TEMP_MAX_C,
    TEMP_MIN_C,
)
from data.product_catalogue import get_product_profile, get_default_product_id

logger = logging.getLogger(__name__)


class RiskLevel(str, Enum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


class RecommendedAction(str, Enum):
    MONITOR_ONLY           = "MONITOR_ONLY"
    ALERT_OPERATIONS       = "ALERT_OPERATIONS"
    REROUTE_SHIPMENT       = "REROUTE_SHIPMENT"
    COLD_STORAGE_RESCUE    = "COLD_STORAGE_RESCUE"
    CUSTOMS_ESCALATION     = "CUSTOMS_ESCALATION"
    NOTIFY_HOSPITAL        = "NOTIFY_HOSPITAL"
    INITIATE_INSURANCE     = "INITIATE_INSURANCE"
    QUARANTINE_PRODUCT     = "QUARANTINE_PRODUCT"
    EMERGENCY_RECALL       = "EMERGENCY_RECALL"


@dataclass
class RiskAssessment:
    shipment_id:    str
    container_id:   str
    assessed_at:    datetime
    risk_score:     float               # 0.0 – 1.0
    risk_level:     RiskLevel
    anomalies:      List[Anomaly]
    actions:        List[RecommendedAction]
    justification:  str
    spoilage_prob:  float               # 0.0 – 1.0 estimated product loss probability
    metadata:       dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "shipment_id":   self.shipment_id,
            "container_id":  self.container_id,
            "assessed_at":   self.assessed_at.isoformat(),
            "risk_score":    round(self.risk_score, 4),
            "risk_level":    self.risk_level.value,
            "anomaly_count": len(self.anomalies),
            "actions":       [a.value for a in self.actions],
            "justification": self.justification,
            "spoilage_prob": round(self.spoilage_prob, 4),
        }


class RiskAgent:
    """
    Scores risk from anomalies + telemetry context and maps to actions.
    """

    def __init__(self, telemetry_agent: Optional[TelemetryAgent] = None):
        self._weights = RISK_WEIGHTS
        self._tel = telemetry_agent

    def assess(self, record: TelemetryRecord, anomalies: List[Anomaly]) -> RiskAssessment:
        score = self._compute_score(record, anomalies)
        level = self._score_to_level(score)
        actions = self._recommend_actions(level, anomalies, record)
        spoilage = self._estimate_spoilage(record, anomalies)
        justification = self._build_justification(record, anomalies, score, level)

        assessment = RiskAssessment(
            shipment_id   = record.shipment_id,
            container_id  = record.container_id,
            assessed_at   = datetime.now(timezone.utc),
            risk_score    = score,
            risk_level    = level,
            anomalies     = anomalies,
            actions       = actions,
            justification = justification,
            spoilage_prob = spoilage,
        )
        logger.info("[%s] Risk=%s (%.2f) → actions: %s",
                    record.shipment_id, level.value, score,
                    [a.value for a in actions])
        return assessment

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _compute_score(self, record: TelemetryRecord, anomalies: List[Anomaly]) -> float:
        components: dict[str, float] = {}

        product_id = (record.raw or {}).get("product_id") or get_default_product_id()
        profile = get_product_profile(product_id)
        temp_min = profile.temp_min_c if profile else TEMP_MIN_C
        temp_max = profile.temp_max_c if profile else TEMP_MAX_C

        # Phase-aware temperature weight.
        # Aircraft holds are actively temperature-controlled: WAIT_TAKEOFF,
        # ENROUTE, and HOLDING are all inside the aircraft.  A sensor reading
        # outside the product's range during those phases does NOT imply spoilage
        # unless the container's own battery-powered cooling has failed.
        # ARRIVED = cargo is on the ground being offloaded → full exposure.
        phase = (record.raw or {}).get("phase", "")
        in_aircraft = phase in ("WAIT_TAKEOFF", "ENROUTE", "HOLDING")

        # Battery drives the container's active cooling unit.
        # Below 60 % it starts to matter; at 0 % the cooling unit is dead.
        battery_pct = max(0.0, min(100.0, record.battery_pct))
        battery_failure_factor = max(0.0, (60.0 - battery_pct) / 60.0)  # 0→1 as battery 60→0%

        if in_aircraft:
            # Normally no temperature risk inside aircraft; rises proportionally
            # only if the container's own cooling is failing (battery dying).
            temp_weight = battery_failure_factor
        else:
            # ARRIVED: cargo is on the ground — full temperature exposure.
            temp_weight = 1.0

        # Temperature component (0-1)
        if record.temperature_c > temp_max:
            excess = (record.temperature_c - temp_max) / 10.0
            components["temperature"] = min(excess + 0.5, 1.0) * temp_weight
        elif record.temperature_c < temp_min:
            excess = (temp_min - record.temperature_c) / 10.0
            components["temperature"] = min(excess + 0.5, 1.0) * temp_weight
        else:
            components["temperature"] = 0.0

        # Humidity component
        from config import HUMIDITY_MAX_PCT
        if record.humidity_pct > HUMIDITY_MAX_PCT:
            components["humidity"] = min((record.humidity_pct - HUMIDITY_MAX_PCT) / 25.0, 1.0)
        else:
            components["humidity"] = 0.0

        # Shock component
        from config import SHOCK_MAX_G
        if record.shock_g > SHOCK_MAX_G:
            components["shock"] = min((record.shock_g - SHOCK_MAX_G) / 5.0, 1.0)
        else:
            components["shock"] = 0.0

        # Delay component.
        # Normalised over 6h — a 3h holding delay should score ~0.5, not 0.03.
        # Delays accumulate cold-chain excursion time regardless of phase.
        components["delay_hours"] = min(record.delay_hours / 6.0, 1.0)

        # Customs component
        components["customs"] = 1.0 if record.customs_status == "HOLD" else 0.0

        # Battery component — powers the container's active cooling unit.
        # Starts contributing at < 60 %, reaches maximum at 0 %.
        components["battery"] = battery_failure_factor

        # Severity multiplier from anomalies
        severity_bonus = 0.0
        for a in anomalies:
            if a.severity == Severity.CRITICAL:
                severity_bonus += 0.15
            elif a.severity == Severity.HIGH:
                severity_bonus += 0.08
            elif a.severity == Severity.MEDIUM:
                severity_bonus += 0.04

        weighted = sum(
            self._weights.get(k, 0) * v for k, v in components.items()
        )
        return min(weighted + severity_bonus, 1.0)

    def _score_to_level(self, score: float) -> RiskLevel:
        if score >= RISK_HIGH_THRESHOLD + 0.15:
            return RiskLevel.CRITICAL
        if score >= RISK_HIGH_THRESHOLD:
            return RiskLevel.HIGH
        if score >= RISK_MEDIUM_THRESHOLD:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    # ------------------------------------------------------------------
    # Action recommendation
    # ------------------------------------------------------------------

    def _recommend_actions(
        self,
        level: RiskLevel,
        anomalies: List[Anomaly],
        record: TelemetryRecord,
    ) -> List[RecommendedAction]:
        actions: List[RecommendedAction] = []
        anomaly_types = {a.anomaly_type for a in anomalies}

        if level == RiskLevel.LOW:
            actions.append(RecommendedAction.MONITOR_ONLY)
            return actions

        actions.append(RecommendedAction.ALERT_OPERATIONS)

        if AnomalyType.TEMP_HIGH in anomaly_types or AnomalyType.TEMP_LOW in anomaly_types:
            actions.append(RecommendedAction.COLD_STORAGE_RESCUE)

        if AnomalyType.SUSTAINED_EXCURSION in anomaly_types:
            actions.append(RecommendedAction.QUARANTINE_PRODUCT)
            actions.append(RecommendedAction.NOTIFY_HOSPITAL)
            actions.append(RecommendedAction.INITIATE_INSURANCE)

        if AnomalyType.FLIGHT_DELAY in anomaly_types or AnomalyType.FLIGHT_DIVERSION in anomaly_types:
            actions.append(RecommendedAction.REROUTE_SHIPMENT)
            actions.append(RecommendedAction.NOTIFY_HOSPITAL)

        if AnomalyType.CUSTOMS_HOLD in anomaly_types:
            actions.append(RecommendedAction.CUSTOMS_ESCALATION)

        if level == RiskLevel.CRITICAL:
            actions.append(RecommendedAction.INITIATE_INSURANCE)
            if RecommendedAction.QUARANTINE_PRODUCT not in actions:
                actions.append(RecommendedAction.QUARANTINE_PRODUCT)

        # EMERGENCY_RECALL: sustained excursion at HIGH or CRITICAL level
        # (sustained exposure even at HIGH risk can render vaccines non-viable)
        if (AnomalyType.SUSTAINED_EXCURSION in anomaly_types
                and level in (RiskLevel.HIGH, RiskLevel.CRITICAL)):
            actions.append(RecommendedAction.EMERGENCY_RECALL)

        return list(dict.fromkeys(actions))  # deduplicate, preserve order

    # ------------------------------------------------------------------
    # Spoilage probability
    # ------------------------------------------------------------------

    def _estimate_spoilage(self, record: TelemetryRecord, anomalies: List[Anomaly]) -> float:
        """
        Estimated spoilage probability (0.0–1.0) using:
        - Mean Kinetic Temperature (MKT) over telemetry history (time-weighted)
        - Product-specific profile thresholds / excursion windows
        """
        product_id = (record.raw or {}).get("product_id") or get_default_product_id()
        profile = get_product_profile(product_id)

        # Compute MKT from temperature history if TelemetryAgent is available.
        history = self._tel.get_history(record.shipment_id) if self._tel else [record]
        mkt_c = self._mean_kinetic_temperature_c(history)

        # Base probability from MKT relative to allowed range.
        # If MKT is within [temp_min, temp_max], start near zero.
        temp_min = profile.temp_min_c if profile else TEMP_MIN_C
        temp_max = profile.temp_max_c if profile else TEMP_MAX_C

        if mkt_c is None:
            base = 0.0
        elif temp_min <= mkt_c <= temp_max:
            base = 0.05 if anomalies else 0.0
        else:
            # Scale by degrees outside range (steeper for ultra-cold chain).
            span = max(1.0, (temp_max - temp_min))
            if mkt_c > temp_max:
                deg = mkt_c - temp_max
            else:
                deg = temp_min - mkt_c
            base = min(0.25 + (deg / max(2.0, span * 0.15)) * 0.25, 0.95)

        # Excursion window: count time above excursion_max_temp_c for refrigerated products
        # (and time above temp_max for ultra-cold).
        excursion_prob = 0.0
        excursion_hours = self._excursion_hours(history, profile)
        allowed = float(profile.excursion_max_hours) if profile else 0.0
        if allowed <= 0 and excursion_hours > 0 and profile and profile.excursion_max_hours <= 0:
            excursion_prob = 0.9
        elif excursion_hours > allowed > 0:
            excursion_prob = min(0.3 + (excursion_hours - allowed) / max(1.0, allowed) * 0.4, 0.9)

        # Severity contributions from anomalies (bounded).
        severity_bonus = 0.0
        for a in anomalies:
            if a.severity == Severity.CRITICAL:
                severity_bonus += 0.10
            elif a.severity == Severity.HIGH:
                severity_bonus += 0.05
            elif a.severity == Severity.MEDIUM:
                severity_bonus += 0.02
        severity_bonus = min(severity_bonus, 0.25)

        prob = max(base, excursion_prob) + severity_bonus
        return min(max(prob, 0.0), 1.0)

    # ------------------------------------------------------------------
    # MKT helpers (P1)
    # ------------------------------------------------------------------

    @staticmethod
    def _mean_kinetic_temperature_c(history: List[TelemetryRecord]) -> Optional[float]:
        """
        Time-weighted Mean Kinetic Temperature (MKT) in °C.
        MKT = ΔH/R / -ln( Σ(wi * exp(-ΔH/(R*Ti))) / Σ(wi) )
        with ΔH = 83,144 J/mol and R = 8.314 J/mol/K.
        """
        if not history:
            return None

        # Need at least 2 points for weighting; fallback to single-point Kelvin.
        if len(history) == 1:
            return float(history[0].temperature_c)

        delta_h = 83144.0
        r_gas = 8.314

        # Ensure chronological order.
        hist = sorted(history, key=lambda x: x.timestamp)
        weights = []
        terms = []
        for i in range(1, len(hist)):
            prev = hist[i - 1]
            cur = hist[i]
            dt = max(1.0, (cur.timestamp - prev.timestamp).total_seconds())
            # Use the later reading as representative for the interval.
            t_k = float(cur.temperature_c) + 273.15
            weights.append(dt)
            terms.append(math.exp(-delta_h / (r_gas * t_k)) * dt)

        denom = sum(weights)
        if denom <= 0:
            return float(hist[-1].temperature_c)

        avg = sum(terms) / denom
        if avg <= 0:
            return float(hist[-1].temperature_c)

        mkt_k = (delta_h / r_gas) / (-math.log(avg))
        return float(mkt_k - 273.15)

    @staticmethod
    def _excursion_hours(history: List[TelemetryRecord], profile) -> float:
        """Compute time in hours above excursion threshold."""
        if len(history) < 2:
            return 0.0
        hist = sorted(history, key=lambda x: x.timestamp)
        threshold = float(profile.excursion_max_temp_c) if profile else float(TEMP_MAX_C)
        total = 0.0
        for i in range(1, len(hist)):
            prev = hist[i - 1]
            cur = hist[i]
            dt = max(0.0, (cur.timestamp - prev.timestamp).total_seconds())
            # Count excursion if the interval endpoint is above threshold.
            if float(cur.temperature_c) > threshold:
                total += dt
        return total / 3600.0

    # ------------------------------------------------------------------

    def _build_justification(
        self,
        record: TelemetryRecord,
        anomalies: List[Anomaly],
        score: float,
        level: RiskLevel,
    ) -> str:
        # Always build the rule-based summary first (used as fallback + LLM context)
        rule_lines = [
            f"Risk level {level.value} (score {score:.2f}) for shipment {record.shipment_id}.",
            f"Detected {len(anomalies)} anomaly/anomalies:",
        ]
        for a in anomalies:
            rule_lines.append(f"  • [{a.severity.value}] {a.description}")
        rule_lines.append(
            f"Current conditions: {record.temperature_c:.1f}°C, "
            f"{record.humidity_pct:.1f}%RH, shock {record.shock_g:.2f}g, "
            f"delay {record.delay_hours:.1f}h, customs={record.customs_status}."
        )
        rule_based = " ".join(rule_lines)

        if not GEMINI_API_KEY:
            return rule_based

        try:
            return self._llm_justify(record, anomalies, score, level, rule_based)
        except Exception as exc:
            logger.warning("Gemini justification failed (%s) — using rule-based fallback", exc)
            return rule_based

    def _llm_justify(
        self,
        record: TelemetryRecord,
        anomalies: List[Anomaly],
        score: float,
        level: RiskLevel,
        rule_based: str,
    ) -> str:
        """Call Gemini to generate a richer natural-language justification."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)

        anomaly_lines = "\n".join(
            f"  - [{a.severity.value}] {a.anomaly_type.value}: {a.description}"
            for a in anomalies
        ) or "  - None"

        prompt = f"""You are a pharmaceutical cold-chain compliance officer.
Analyse this shipment event and write a concise 2-3 sentence operational justification
explaining the risk level, its impact on vaccine viability, and the urgency of action.
Be specific, cite the anomalies, and use GDP/FDA regulatory language where relevant.

Shipment ID   : {record.shipment_id}
Risk Level    : {level.value}  (score {score:.2f} / 1.00)
Temperature   : {record.temperature_c:.1f}°C  (safe range: {TEMP_MIN_C}–{TEMP_MAX_C}°C)
Humidity      : {record.humidity_pct:.1f}%RH
Shock         : {record.shock_g:.2f}g
Delay         : {record.delay_hours:.1f}h
Customs       : {record.customs_status}
Flight Status : {record.flight_status}
Anomalies detected:
{anomaly_lines}

Write only the justification paragraph. No headings, no bullet points."""

        response = client.models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=LLM_TEMPERATURE,
                max_output_tokens=300,
            ),
        )
        text = response.text.strip()
        logger.debug("[%s] Gemini justification generated (%d chars)", record.shipment_id, len(text))
        return text
