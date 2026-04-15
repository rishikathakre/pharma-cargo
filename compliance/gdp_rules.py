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

        return {
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
