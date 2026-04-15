"""
GDPValidator
------------
Validates telemetry records and risk assessments against:
  - EU Good Distribution Practice (GDP) 2013/C 343/01
  - US FDA 21 CFR Part 211 / 600 (biologics storage & distribution)

Returns (is_compliant: bool, violations: List[str]).
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from agents.telemetry_agent import TelemetryRecord
from agents.risk_agent import RiskAssessment, RiskLevel
from config import (
    TEMP_MAX_C,
    TEMP_MIN_C,
    HUMIDITY_MAX_PCT,
    SHOCK_MAX_G,
    GDP_REGULATION,
    FDA_REGULATION,
)

logger = logging.getLogger(__name__)

# GDP article references (for audit trail)
GDP_TEMP_ARTICLE      = "GDP §9.2 – Temperature Conditions"
GDP_HUMIDITY_ARTICLE  = "GDP §9.2 – Humidity Conditions"
GDP_SHOCK_ARTICLE     = "GDP §9.3 – Mechanical Integrity"
GDP_CUSTOMS_ARTICLE   = "GDP §7.1 – Import Operations"
GDP_DELAY_ARTICLE     = "GDP §9.2 – Transit Duration"
FDA_STORAGE_RULE      = "21 CFR 211.142 – Storage of Drug Products"
FDA_BIOLOGICS_RULE    = "21 CFR 600.15 – Temperature During Shipment"


class GDPValidator:
    """
    Rule-based compliance checker.
    Extend with product-specific monograph limits for production use.
    """

    def validate(
        self,
        record: TelemetryRecord,
        assessment: RiskAssessment,
    ) -> Tuple[bool, List[str]]:
        violations: List[str] = []

        violations.extend(self._check_temperature(record))
        violations.extend(self._check_humidity(record))
        violations.extend(self._check_shock(record))
        violations.extend(self._check_customs(record))
        violations.extend(self._check_delay(record))
        violations.extend(self._check_critical_no_action(assessment))

        compliant = len(violations) == 0
        if not compliant:
            logger.warning("[%s] GDP/FDA violations: %s",
                           record.shipment_id, violations)
        return compliant, violations

    # ------------------------------------------------------------------

    def _check_temperature(self, record: TelemetryRecord) -> List[str]:
        v = []
        if record.temperature_c > TEMP_MAX_C:
            v.append(
                f"{GDP_TEMP_ARTICLE} | {FDA_BIOLOGICS_RULE}: "
                f"Temperature {record.temperature_c:.1f}°C exceeds {TEMP_MAX_C}°C"
            )
        if record.temperature_c < TEMP_MIN_C:
            v.append(
                f"{GDP_TEMP_ARTICLE} | {FDA_BIOLOGICS_RULE}: "
                f"Temperature {record.temperature_c:.1f}°C below {TEMP_MIN_C}°C"
            )
        return v

    def _check_humidity(self, record: TelemetryRecord) -> List[str]:
        if record.humidity_pct > HUMIDITY_MAX_PCT:
            return [
                f"{GDP_HUMIDITY_ARTICLE}: "
                f"Humidity {record.humidity_pct:.1f}% exceeds {HUMIDITY_MAX_PCT}%"
            ]
        return []

    def _check_shock(self, record: TelemetryRecord) -> List[str]:
        if record.shock_g > SHOCK_MAX_G:
            return [
                f"{GDP_SHOCK_ARTICLE}: "
                f"Shock {record.shock_g:.2f}g exceeds limit {SHOCK_MAX_G}g"
            ]
        return []

    def _check_customs(self, record: TelemetryRecord) -> List[str]:
        if record.customs_status == "HOLD":
            return [
                f"{GDP_CUSTOMS_ARTICLE}: "
                f"Shipment {record.shipment_id} is under customs HOLD; "
                f"cold-chain continuity at risk"
            ]
        return []

    def _check_delay(self, record: TelemetryRecord) -> List[str]:
        # GDP recommends that delays exposing cold-chain product > 6 hours
        # must be documented and risk-assessed.
        if record.delay_hours > 6:
            return [
                f"{GDP_DELAY_ARTICLE}: "
                f"Transit delay of {record.delay_hours:.1f}h requires documented "
                f"risk assessment per GDP §9.2"
            ]
        return []

    def _check_critical_no_action(self, assessment: RiskAssessment) -> List[str]:
        """CRITICAL risk with no approved actions is a compliance gap."""
        from agents.risk_agent import RecommendedAction
        if assessment.risk_level == RiskLevel.CRITICAL:
            safe_actions = {
                RecommendedAction.MONITOR_ONLY,
                RecommendedAction.ALERT_OPERATIONS,
            }
            if set(assessment.actions).issubset(safe_actions):
                return [
                    f"GDP §1.4 – Pharmaceutical Quality System: "
                    f"CRITICAL risk detected but only passive actions recommended — "
                    f"immediate escalation required"
                ]
        return []

    def generate_compliance_report(
        self,
        shipment_id: str,
        records: List[TelemetryRecord],
        assessments: List[RiskAssessment],
    ) -> dict:
        """Generate a summary compliance report for a shipment."""
        all_violations: List[str] = []
        for rec, ass in zip(records, assessments):
            _, violations = self.validate(rec, ass)
            all_violations.extend(violations)

        structured = {
            "shipment_id":    shipment_id,
            "regulation":     f"{GDP_REGULATION} | {FDA_REGULATION}",
            "total_readings": len(records),
            "violations":     all_violations,
            "compliant":      len(all_violations) == 0,
            "summary":        (
                "COMPLIANT" if len(all_violations) == 0
                else f"NON-COMPLIANT ({len(all_violations)} violation(s))"
            ),
        }
        return structured

    def generate_narrative_report(
        self,
        shipment_id: str,
        records: List[TelemetryRecord],
        assessments: List[RiskAssessment],
    ) -> dict:
        """
        Generate a full narrative compliance report using Gemini LLM.
        Falls back to structured-only if LLM is unavailable.
        """
        structured = self.generate_compliance_report(shipment_id, records, assessments)

        from config import GEMINI_API_KEY, LLM_MODEL, LLM_TEMPERATURE, LLM_MAX_RETRIES, LLM_BACKOFF_BASE_SEC
        if not GEMINI_API_KEY:
            structured["narrative"] = self._rule_based_narrative(structured)
            structured["narrative_source"] = "rule_based"
            return structured

        import time as _time
        for attempt in range(LLM_MAX_RETRIES):
            try:
                from google import genai
                from google.genai import types

                client = genai.Client(api_key=GEMINI_API_KEY)

                # Build context for the LLM
                violation_text = "\n".join(
                    f"  - {v}" for v in structured["violations"]
                ) or "  - None"

                # Summarize readings
                if records:
                    temps = [r.temperature_c for r in records]
                    humids = [r.humidity_pct for r in records]
                    delays = [r.delay_hours for r in records]
                    reading_summary = (
                        f"Temperature range: {min(temps):.1f}°C – {max(temps):.1f}°C (safe: {TEMP_MIN_C}–{TEMP_MAX_C}°C)\n"
                        f"Humidity range: {min(humids):.1f}% – {max(humids):.1f}% (limit: {HUMIDITY_MAX_PCT}%)\n"
                        f"Max delay: {max(delays):.1f}h\n"
                        f"Total readings: {len(records)}"
                    )
                else:
                    reading_summary = "No telemetry readings available."

                # Risk summary
                if assessments:
                    max_risk = max(a.risk_score for a in assessments)
                    risk_levels = [a.risk_level.value for a in assessments]
                    highest_level = max(risk_levels, key=lambda x: ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(x))
                    spoilage_probs = [a.spoilage_prob for a in assessments]
                    risk_summary = (
                        f"Peak risk score: {max_risk:.2f} ({highest_level})\n"
                        f"Max spoilage probability: {max(spoilage_probs):.1%}\n"
                        f"Assessments: {len(assessments)}"
                    )
                else:
                    risk_summary = "No risk assessments available."

                prompt = f"""You are a pharmaceutical regulatory compliance officer writing an official
GDP/FDA compliance report for a cold-chain shipment. Write a formal, audit-ready narrative report
that could be submitted to regulatory authorities.

The report must include:
1. Executive Summary (2-3 sentences)
2. Shipment Overview (ID, regulation, readings count)
3. Compliance Findings (violations found, with specific GDP/FDA article references)
4. Risk Assessment Summary (peak risk, spoilage probability)
5. Corrective Actions Taken (based on the violations)
6. Conclusion and Recommendation

Use formal regulatory language. Cite specific GDP articles and FDA CFR sections.
Be precise with numbers. Do NOT invent data — use only what is provided below.

SHIPMENT ID: {shipment_id}
APPLICABLE REGULATIONS: {GDP_REGULATION} | {FDA_REGULATION}
COMPLIANCE STATUS: {structured['summary']}

TELEMETRY SUMMARY:
{reading_summary}

RISK SUMMARY:
{risk_summary}

VIOLATIONS DETECTED ({len(structured['violations'])}):
{violation_text}

Write the complete report. Use markdown headers (##) for sections."""

                response = client.models.generate_content(
                    model=LLM_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=LLM_TEMPERATURE,
                        max_output_tokens=1500,
                    ),
                )
                narrative = response.text.strip()
                structured["narrative"] = narrative
                structured["narrative_source"] = "gemini"
                logger.info("[%s] Narrative compliance report generated (%d chars)",
                           shipment_id, len(narrative))
                return structured

            except Exception as exc:
                exc_str = str(exc)
                is_rate_limit = "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str
                if is_rate_limit and attempt < LLM_MAX_RETRIES - 1:
                    wait = LLM_BACKOFF_BASE_SEC * (2 ** attempt)
                    logger.info("[%s] Gemini 429 on compliance report — retrying in %.1fs",
                               shipment_id, wait)
                    _time.sleep(wait)
                else:
                    logger.warning("Compliance report LLM failed (%s) — using rule-based", exc)
                    structured["narrative"] = self._rule_based_narrative(structured)
                    structured["narrative_source"] = "rule_based_fallback"
                    return structured

        structured["narrative"] = self._rule_based_narrative(structured)
        structured["narrative_source"] = "rule_based_fallback"
        return structured

    def _rule_based_narrative(self, structured: dict) -> str:
        """Generate a basic narrative without LLM."""
        lines = [
            f"## Compliance Report — Shipment {structured['shipment_id']}",
            f"",
            f"**Applicable Regulations:** {structured['regulation']}",
            f"**Total Readings:** {structured['total_readings']}",
            f"**Status:** {structured['summary']}",
            f"",
        ]
        if structured["violations"]:
            lines.append("### Violations")
            for v in structured["violations"]:
                lines.append(f"- {v}")
        else:
            lines.append("### Findings")
            lines.append("No violations detected. Shipment maintained compliance throughout transit.")
        return "\n".join(lines)
