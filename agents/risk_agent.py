"""
RiskAgent
---------
Aggregates anomalies into a composite RiskAssessment score (0-1) and
recommends a prioritised list of actions.  Uses a weighted scoring model
and optionally calls an LLM for natural-language justification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from agents.anomaly_agent import Anomaly, AnomalyType, Severity
from agents.telemetry_agent import TelemetryRecord
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

    def __init__(self):
        self._weights = RISK_WEIGHTS

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

        # Temperature component (0-1)
        if record.temperature_c > TEMP_MAX_C:
            excess = (record.temperature_c - TEMP_MAX_C) / 10.0
            components["temperature"] = min(excess + 0.5, 1.0)
        elif record.temperature_c < TEMP_MIN_C:
            excess = (TEMP_MIN_C - record.temperature_c) / 10.0
            components["temperature"] = min(excess + 0.5, 1.0)
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

        # Delay component (normalised over 24h)
        components["delay_hours"] = min(record.delay_hours / 24.0, 1.0)

        # Customs component
        components["customs"] = 1.0 if record.customs_status == "HOLD" else 0.0

        # Severity multiplier from anomalies
        severity_bonus = 0.0
        for a in anomalies:
            if a.severity == Severity.CRITICAL:
                severity_bonus += 0.15
            elif a.severity == Severity.HIGH:
                severity_bonus += 0.08

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
        Returns estimated spoilage probability (0.0–1.0).

        TODO: Replace this stub with a pharmacological model grounded in real data:
          - Mean Kinetic Temperature (MKT) per WHO TRS 961 Annex 9 / USP <1079>:
              MKT = ΔH/R / -ln( Σ exp(-ΔH/RTi) / n )
              where ΔH = activation energy (~83.14 kJ/mol for most vaccines),
              R = gas constant, Ti = temperature at each reading (Kelvin).
          - Product-specific stability profiles: each vaccine/biologic has its own
              approved excursion window (e.g., mRNA vaccines: 2 hours at 8–25°C;
              MMR: 72 hours at ≤25°C). These come from the product monograph or SmPC.
          - Integrate cumulative time-temperature exposure (TTI) from TelemetryAgent
              history, not just the current snapshot.
          - Shock damage probability should use product-specific fragility ratings.

        Until real data and product profiles are available, spoilage_prob = 0.0
        so downstream financial calculations (insurance, inventory) are not
        misleadingly populated with arbitrary heuristic numbers.
        """
        return 0.0

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
