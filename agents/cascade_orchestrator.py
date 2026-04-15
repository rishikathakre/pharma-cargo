"""
CascadeOrchestrator
-------------------
LangGraph-based orchestration graph that wires together:
  Telemetry → Anomaly Detection → Risk Assessment → HITL Gate → Action Dispatch → Compliance Log

State flows through typed nodes; edges decide whether to escalate to HITL
or execute actions automatically based on risk level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# LangGraph imports
from langgraph.graph import StateGraph, END

from agents.action_agent import ActionAgent, ActionResult
from agents.anomaly_agent import Anomaly, AnomalyAgent, AnomalyType
from agents.risk_agent import RecommendedAction, RiskAgent, RiskAssessment, RiskLevel
from agents.reroute_engine import RerouteEngine
from agents.telemetry_agent import TelemetryAgent, TelemetryRecord
from compliance.audit_logger import AuditLogger
from compliance.gdp_rules import GDPValidator
from config import AFFECTED_VACCINE_TYPES, HITL_AUTO_APPROVE_LOW
from hitl.approval_queue import ApprovalQueue, ApprovalRequest, ApprovalStatus
from notifications.hospital_notifier import HospitalNotifier
from notifications.insurance_docs import InsuranceDocGenerator
from notifications.inventory_updater import InventoryUpdater

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Graph state (typed dict passed between nodes)
# ---------------------------------------------------------------------------

@dataclass
class PipelineState:
    raw_payload:   Dict[str, Any]                      = field(default_factory=dict)
    record:        Optional[TelemetryRecord]            = None
    anomalies:     List[Anomaly]                        = field(default_factory=list)
    assessment:    Optional[RiskAssessment]             = None
    hitl_request:  Optional[ApprovalRequest]            = None
    approved_actions: List[RecommendedAction]           = field(default_factory=list)
    action_results:   List[ActionResult]                = field(default_factory=list)
    compliant:     bool                                 = True
    errors:        List[str]                            = field(default_factory=list)
    completed_at:  Optional[datetime]                   = None


def state_to_dict(s: PipelineState) -> Dict[str, Any]:
    return {
        "shipment_id":    s.record.shipment_id if s.record else None,
        "risk_level":     s.assessment.risk_level.value if s.assessment else None,
        "risk_score":     s.assessment.risk_score if s.assessment else None,
        "anomaly_count":  len(s.anomalies),
        "approved_actions": [a.value for a in s.approved_actions],
        "compliant":      s.compliant,
        "errors":         s.errors,
        "completed_at":   s.completed_at.isoformat() if s.completed_at else None,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class CascadeOrchestrator:
    """
    Builds and runs the LangGraph pipeline for a single telemetry event.
    """

    def __init__(
        self,
        telemetry_agent:  Optional[TelemetryAgent]  = None,
        anomaly_agent:    Optional[AnomalyAgent]    = None,
        risk_agent:       Optional[RiskAgent]       = None,
        action_agent:     Optional[ActionAgent]     = None,
        approval_queue:   Optional[ApprovalQueue]   = None,
        audit_logger:     Optional[AuditLogger]     = None,
        gdp_validator:    Optional[GDPValidator]    = None,
        hospital_notifier: Optional[HospitalNotifier] = None,
        insurance_docs:   Optional[InsuranceDocGenerator] = None,
        inventory_updater: Optional[InventoryUpdater] = None,
    ):
        self.tel     = telemetry_agent  or TelemetryAgent()
        self.ano     = anomaly_agent    or AnomalyAgent(self.tel)
        self.risk    = risk_agent       or RiskAgent(telemetry_agent=self.tel)
        self.act     = action_agent     or ActionAgent()
        self.hitl    = approval_queue   or ApprovalQueue()
        self.audit   = audit_logger     or AuditLogger()
        self.gdp     = gdp_validator    or GDPValidator()
        self.hosp    = hospital_notifier  or HospitalNotifier()
        self.ins     = insurance_docs     or InsuranceDocGenerator()
        self.inv     = inventory_updater  or InventoryUpdater()
        self.reroute = RerouteEngine()

        self._register_action_handlers()
        self._graph = self._build_graph()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, raw_payload: Dict[str, Any]) -> PipelineState:
        initial = {"raw_payload": raw_payload, "anomalies": [], "approved_actions": [],
                   "action_results": [], "errors": [], "compliant": True}
        final = self._graph.invoke(initial)
        # Convert dict back to PipelineState for typed access
        state = PipelineState(**{k: v for k, v in final.items()
                                  if k in PipelineState.__dataclass_fields__})
        state.completed_at = datetime.now(timezone.utc)
        self.audit.log_pipeline_run(state_to_dict(state))
        return state

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _node_ingest(self, state: Dict) -> Dict:
        try:
            record = self.tel.ingest(state["raw_payload"])
            state["record"] = record
            logger.debug("Node ingest: %s", record.shipment_id)
        except Exception as e:
            state["errors"].append(f"ingest: {e}")
        return state

    def _node_detect_anomalies(self, state: Dict) -> Dict:
        record = state.get("record")
        if record is None:
            return state
        try:
            anomalies = self.ano.analyse(record)
            state["anomalies"] = anomalies
            for anomaly in anomalies:
                self.audit.log_anomaly(anomaly)
        except Exception as e:
            state["errors"].append(f"anomaly_detect: {e}")
        return state

    def _node_assess_risk(self, state: Dict) -> Dict:
        record    = state.get("record")
        anomalies = state.get("anomalies", [])
        if record is None:
            return state
        try:
            assessment = self.risk.assess(record, anomalies)
            # Inject route metadata from raw payload so action handlers can use it
            assessment.metadata.update({
                "carrier":     record.raw.get("carrier", "Unknown"),
                "origin":      record.raw.get("origin",  "Unknown"),
                "destination": record.raw.get("destination", "Unknown"),
            })
            state["assessment"] = assessment
            # Auto-approve MONITOR_ONLY for no-anomaly LOW risk — bypasses HITL gate
            if not anomalies and HITL_AUTO_APPROVE_LOW and assessment.risk_level.value == "LOW":
                state["approved_actions"] = assessment.actions
                logger.info("[%s] No-anomaly LOW risk — auto-approved: %s",
                            assessment.shipment_id, [a.value for a in assessment.actions])
        except Exception as e:
            state["errors"].append(f"risk_assess: {e}")
        return state

    def _node_gdp_check(self, state: Dict) -> Dict:
        record     = state.get("record")
        assessment = state.get("assessment")
        if record is None or assessment is None:
            return state
        try:
            compliant, violations = self.gdp.validate(record, assessment)
            state["compliant"] = compliant
            if violations:
                for v in violations:
                    self.audit.log_compliance_violation(record.shipment_id, v)
        except Exception as e:
            state["errors"].append(f"gdp_check: {e}")
        return state

    def _node_hitl_gate(self, state: Dict) -> Dict:
        assessment = state.get("assessment")
        if assessment is None:
            return state
        try:
            if HITL_AUTO_APPROVE_LOW and assessment.risk_level == RiskLevel.LOW:
                state["approved_actions"] = assessment.actions
                logger.info("[%s] Auto-approved LOW risk actions", assessment.shipment_id)
            else:
                req = self.hitl.submit(assessment)
                state["hitl_request"] = req
                # Block until resolved (with timeout fallback)
                resolved = self.hitl.wait_for_decision(req.request_id)
                self.audit.log_hitl_decision(resolved)   # ALCOA+ — every decision logged
                if resolved.status == ApprovalStatus.REJECTED:
                    state["approved_actions"] = []
                    logger.warning("[%s] Actions REJECTED by HITL", assessment.shipment_id)
                else:
                    # APPROVED, PARTIAL, or TIMEOUT — execute whatever was approved
                    state["approved_actions"] = resolved.approved_actions
                    if resolved.status == ApprovalStatus.TIMEOUT:
                        logger.warning(
                            "[%s] HITL timeout — executing safe auto-approved actions: %s",
                            assessment.shipment_id,
                            [a.value for a in resolved.approved_actions],
                        )
        except Exception as e:
            state["errors"].append(f"hitl_gate: {e}")
        return state

    def _node_execute_actions(self, state: Dict) -> Dict:
        assessment      = state.get("assessment")
        approved_actions = state.get("approved_actions", [])
        if assessment is None or not approved_actions:
            return state
        try:
            results = self.act.execute(assessment, approved_actions)
            state["action_results"] = results
        except Exception as e:
            state["errors"].append(f"execute_actions: {e}")
        return state

    def _node_audit_log(self, state: Dict) -> Dict:
        assessment = state.get("assessment")
        results    = state.get("action_results", [])
        if assessment:
            self.audit.log_assessment(assessment)
            for r in results:
                self.audit.log_action_result(r)
        return state

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _route_after_risk(self, state: Dict) -> str:
        assessment = state.get("assessment")
        if assessment is None or not assessment.anomalies:
            return "audit_log"   # no anomalies — state already updated in _node_assess_risk
        return "gdp_check"

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self) -> StateGraph:
        g = StateGraph(dict)

        g.add_node("ingest",          self._node_ingest)
        g.add_node("detect_anomalies",self._node_detect_anomalies)
        g.add_node("assess_risk",     self._node_assess_risk)
        g.add_node("gdp_check",       self._node_gdp_check)
        g.add_node("hitl_gate",       self._node_hitl_gate)
        g.add_node("execute_actions", self._node_execute_actions)
        g.add_node("audit_log",       self._node_audit_log)

        g.set_entry_point("ingest")
        g.add_edge("ingest",           "detect_anomalies")
        g.add_edge("detect_anomalies", "assess_risk")
        g.add_conditional_edges("assess_risk", self._route_after_risk,
                                 {"gdp_check": "gdp_check", "audit_log": "audit_log"})
        g.add_edge("gdp_check",        "hitl_gate")
        g.add_edge("hitl_gate",        "execute_actions")
        g.add_edge("execute_actions",  "audit_log")
        g.add_edge("audit_log",        END)

        return g.compile()

    # ------------------------------------------------------------------
    # Action handler wiring
    # ------------------------------------------------------------------

    def _register_action_handlers(self) -> None:
        from agents.risk_agent import RecommendedAction as RA

        self.act.register_handler(RA.NOTIFY_HOSPITAL,
            lambda a: self._handle_notify_hospital(a))
        self.act.register_handler(RA.INITIATE_INSURANCE,
            lambda a: self.ins.generate(a))
        self.act.register_handler(RA.COLD_STORAGE_RESCUE,
            lambda a: self._handle_cold_storage_rescue(a))
        self.act.register_handler(RA.REROUTE_SHIPMENT,
            lambda a: self.reroute.suggest(a))
        self.act.register_handler(RA.CUSTOMS_ESCALATION,
            lambda a: self.reroute.escalate_customs(a))
        self.act.register_handler(RA.QUARANTINE_PRODUCT,
            lambda a: self._handle_quarantine(a))
        self.act.register_handler(RA.ALERT_OPERATIONS,
            lambda a: self._handle_alert_operations(a))
        self.act.register_handler(RA.EMERGENCY_RECALL,
            lambda a: self._handle_emergency_recall(a))
        self.act.register_handler(RA.MONITOR_ONLY,
            lambda a: {"status": "monitoring", "shipment_id": a.shipment_id,
                       "risk_score": round(a.risk_score, 4)})

    # ------------------------------------------------------------------
    # Cascading action implementations
    # ------------------------------------------------------------------

    def _handle_notify_hospital(self, assessment: RiskAssessment) -> dict:
        """Notify hospital and auto-trigger appointment reschedule on delays."""
        result = self.hosp.notify(assessment)

        # Extract delay from anomalies and trigger reschedule if needed
        delay_hours = 0.0
        has_delay   = False
        for a in assessment.anomalies:
            if a.anomaly_type in (AnomalyType.FLIGHT_DELAY, AnomalyType.FLIGHT_DIVERSION):
                has_delay = True
                if a.measured_value:
                    delay_hours = max(delay_hours, a.measured_value)

        if has_delay and delay_hours > 0:
            destination = assessment.metadata.get("destination", "destination")
            reschedule  = self.hosp.notify_appointment_reschedule(
                shipment_id      = assessment.shipment_id,
                delay_hours      = delay_hours,
                affected_vaccines= AFFECTED_VACCINE_TYPES,
                clinic_id        = destination,
            )
            result["appointment_reschedule"] = reschedule
            logger.info(
                "[%s] Appointment reschedule triggered: %.1fh delay → clinic %s",
                assessment.shipment_id, delay_hours, destination,
            )

        return result

    def _handle_cold_storage_rescue(self, assessment: RiskAssessment) -> dict:
        """Request cold-storage rescue and update downstream inventory forecast."""
        rescue   = self.inv.update_cold_storage(assessment)
        forecast = self.inv.update_forecast(
            shipment_id   = assessment.shipment_id,
            delay_hours   = assessment.metadata.get("delay_hours", 0.0),
            spoilage_prob = assessment.spoilage_prob,
            product_ids   = ["VACCINE-COLD-CHAIN"],
        )
        return {"cold_storage": rescue, "forecast_update": forecast}

    def _handle_quarantine(self, assessment: RiskAssessment) -> dict:
        """Quarantine product and update inventory forecast to zero expected stock."""
        quarantine = self.inv.quarantine(assessment)
        forecast   = self.inv.update_forecast(
            shipment_id   = assessment.shipment_id,
            delay_hours   = 0.0,
            spoilage_prob = 1.0,   # full loss assumed on quarantine
            product_ids   = ["VACCINE-COLD-CHAIN"],
        )
        return {"quarantine": quarantine, "forecast_update": forecast}

    def _handle_alert_operations(self, assessment: RiskAssessment) -> dict:
        return {
            "status":      "ops_alerted",
            "shipment_id": assessment.shipment_id,
            "risk_level":  assessment.risk_level.value,
            "risk_score":  round(assessment.risk_score, 4),
            "carrier":     assessment.metadata.get("carrier", "Unknown"),
            "destination": assessment.metadata.get("destination", "Unknown"),
            "anomaly_count": len(assessment.anomalies),
            "top_anomalies": [
                a.anomaly_type.value for a in assessment.anomalies[:3]
            ],
        }

    def _handle_emergency_recall(self, assessment: RiskAssessment) -> dict:
        """Coordinate full emergency recall: quarantine + insurance + hospital alert."""
        quarantine = self.inv.quarantine(assessment)
        insurance  = self.ins.generate(assessment)
        hospital   = self.hosp.notify(assessment)
        logger.warning(
            "[%s] EMERGENCY RECALL initiated — spoilage_prob=%.2f",
            assessment.shipment_id, assessment.spoilage_prob,
        )
        return {
            "status":     "EMERGENCY_RECALL_INITIATED",
            "shipment_id": assessment.shipment_id,
            "quarantine":  quarantine,
            "insurance":   insurance,
            "hospital":    hospital,
            "regulatory_note": (
                "Emergency recall documented per GDP §8 – Self-Inspection "
                "and FDA 21 CFR 7 – Enforcement Policy."
            ),
        }
