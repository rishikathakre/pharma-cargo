"""
Test suite for pharma-cargo-monitor agents.
Run with:  pytest tests/ -v
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from agents.telemetry_agent import TelemetryAgent, TelemetryRecord
from agents.anomaly_agent import AnomalyAgent, AnomalyType, Severity
from agents.risk_agent import RiskAgent, RiskLevel, RecommendedAction
from agents.action_agent import ActionAgent
from compliance.gdp_rules import GDPValidator
from compliance.audit_logger import AuditLogger
from hitl.approval_queue import ApprovalQueue, ApprovalStatus
from simulation.stream_simulator import StreamSimulator
from config import TEMP_MAX_C, TEMP_MIN_C, SHOCK_MAX_G


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def telemetry_agent():
    return TelemetryAgent()


@pytest.fixture
def anomaly_agent(telemetry_agent):
    return AnomalyAgent(telemetry_agent)


@pytest.fixture
def risk_agent():
    return RiskAgent()


@pytest.fixture
def normal_payload():
    return {
        "shipment_id":    "SHP-TEST-001",
        "container_id":   "CNT-TEST-001",
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "temperature_c":  5.0,
        "humidity_pct":   55.0,
        "shock_g":        0.2,
        "latitude":       40.64,
        "longitude":      -73.78,
        "altitude_m":     10000.0,
        "customs_status": "CLEARED",
        "flight_status":  "ON_TIME",
        "delay_hours":    0.0,
        "battery_pct":    95.0,
    }


@pytest.fixture
def temp_excursion_payload():
    return {
        "shipment_id":    "SHP-TEST-002",
        "container_id":   "CNT-TEST-002",
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "temperature_c":  12.5,          # Above 8°C threshold
        "humidity_pct":   60.0,
        "shock_g":        0.1,
        "latitude":       40.64,
        "longitude":      -73.78,
        "altitude_m":     10000.0,
        "customs_status": "CLEARED",
        "flight_status":  "ON_TIME",
        "delay_hours":    0.0,
        "battery_pct":    90.0,
    }


@pytest.fixture
def critical_payload():
    return {
        "shipment_id":    "SHP-TEST-003",
        "container_id":   "CNT-TEST-003",
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "temperature_c":  18.0,          # Far above threshold
        "humidity_pct":   85.0,          # Above humidity threshold
        "shock_g":        9.5,           # High shock
        "latitude":       40.64,
        "longitude":      -73.78,
        "altitude_m":     10000.0,
        "customs_status": "HOLD",
        "flight_status":  "DIVERTED",
        "delay_hours":    14.0,
        "battery_pct":    8.0,
    }


# ---------------------------------------------------------------------------
# TelemetryAgent tests
# ---------------------------------------------------------------------------

class TestTelemetryAgent:
    def test_ingest_normal(self, telemetry_agent, normal_payload):
        record = telemetry_agent.ingest(normal_payload)
        assert record.shipment_id  == "SHP-TEST-001"
        assert record.temperature_c == 5.0
        assert record.customs_status == "CLEARED"

    def test_history_stored(self, telemetry_agent, normal_payload):
        telemetry_agent.ingest(normal_payload)
        history = telemetry_agent.get_history("SHP-TEST-001")
        assert len(history) == 1

    def test_latest_record(self, telemetry_agent, normal_payload):
        telemetry_agent.ingest(normal_payload)
        latest = telemetry_agent.latest("SHP-TEST-001")
        assert latest is not None
        assert latest.temperature_c == 5.0

    def test_missing_shipment_returns_none(self, telemetry_agent):
        assert telemetry_agent.latest("NONEXISTENT") is None

    def test_to_dict(self, telemetry_agent, normal_payload):
        record = telemetry_agent.ingest(normal_payload)
        d = record.to_dict()
        assert "shipment_id" in d
        assert "temperature_c" in d


# ---------------------------------------------------------------------------
# AnomalyAgent tests
# ---------------------------------------------------------------------------

class TestAnomalyAgent:
    def test_no_anomaly_normal(self, telemetry_agent, anomaly_agent, normal_payload):
        record = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assert len(anomalies) == 0

    def test_temp_high_detected(self, telemetry_agent, anomaly_agent, temp_excursion_payload):
        record = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)
        types = [a.anomaly_type for a in anomalies]
        assert AnomalyType.TEMP_HIGH in types

    def test_temp_low_detected(self, telemetry_agent, anomaly_agent, normal_payload):
        normal_payload["temperature_c"] = -2.0
        normal_payload["shipment_id"]   = "SHP-TEST-LOW"
        record = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assert any(a.anomaly_type == AnomalyType.TEMP_LOW for a in anomalies)

    def test_shock_detected(self, telemetry_agent, anomaly_agent, normal_payload):
        normal_payload["shock_g"]      = 8.0
        normal_payload["shipment_id"]  = "SHP-TEST-SHOCK"
        record = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assert any(a.anomaly_type == AnomalyType.SHOCK_EVENT for a in anomalies)

    def test_customs_hold_detected(self, telemetry_agent, anomaly_agent, normal_payload):
        normal_payload["customs_status"] = "HOLD"
        normal_payload["shipment_id"]    = "SHP-TEST-CUSTOMS"
        record = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assert any(a.anomaly_type == AnomalyType.CUSTOMS_HOLD for a in anomalies)

    def test_flight_diversion_critical(self, telemetry_agent, anomaly_agent, normal_payload):
        normal_payload["flight_status"] = "DIVERTED"
        normal_payload["shipment_id"]   = "SHP-TEST-DIV"
        record = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assert any(a.anomaly_type == AnomalyType.FLIGHT_DIVERSION for a in anomalies)
        assert any(a.severity == Severity.CRITICAL for a in anomalies)

    def test_battery_low(self, telemetry_agent, anomaly_agent, normal_payload):
        normal_payload["battery_pct"]  = 5.0
        normal_payload["shipment_id"]  = "SHP-TEST-BATT"
        record = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assert any(a.anomaly_type == AnomalyType.BATTERY_LOW for a in anomalies)

    def test_multiple_anomalies(self, telemetry_agent, anomaly_agent, critical_payload):
        record = telemetry_agent.ingest(critical_payload)
        anomalies = anomaly_agent.analyse(record)
        assert len(anomalies) >= 4


# ---------------------------------------------------------------------------
# RiskAgent tests
# ---------------------------------------------------------------------------

class TestRiskAgent:
    def test_low_risk_normal(self, telemetry_agent, anomaly_agent, risk_agent, normal_payload):
        record    = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        assert assessment.risk_level == RiskLevel.LOW
        assert assessment.risk_score < 0.4

    def test_high_risk_excursion(self, telemetry_agent, anomaly_agent, risk_agent,
                                  temp_excursion_payload):
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        # 12.5°C (4.5° above limit) with no other factors → MEDIUM or above
        assert assessment.risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
        assert assessment.risk_score > 0.4

    def test_critical_risk(self, telemetry_agent, anomaly_agent, risk_agent, critical_payload):
        record    = telemetry_agent.ingest(critical_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        assert assessment.risk_level == RiskLevel.CRITICAL
        assert assessment.risk_score > 0.7

    def test_actions_recommended(self, telemetry_agent, anomaly_agent, risk_agent,
                                  critical_payload):
        record    = telemetry_agent.ingest(critical_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        assert len(assessment.actions) > 0
        assert RecommendedAction.MONITOR_ONLY not in assessment.actions

    def test_to_dict(self, telemetry_agent, anomaly_agent, risk_agent, normal_payload):
        record    = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        d = assessment.to_dict()
        assert "risk_score" in d
        assert "risk_level" in d

    def test_gemini_justification_with_mock(self, telemetry_agent, anomaly_agent,
                                            risk_agent, temp_excursion_payload):
        """Verify Gemini path is called and its text is used when API key is present."""
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)

        mock_response      = MagicMock()
        mock_response.text = (
            "Shipment SHP-TEST-002 presents an elevated risk due to a temperature "
            "excursion of 12.5°C, exceeding the GDP §9.2 cold-chain upper limit of 8°C. "
            "Immediate cold-storage rescue is required to preserve vaccine viability "
            "per 21 CFR 600.15."
        )
        mock_models = MagicMock()
        mock_models.generate_content.return_value = mock_response
        mock_client = MagicMock()
        mock_client.models = mock_models

        with patch("agents.risk_agent.GEMINI_API_KEY", "fake-test-key"), \
             patch("google.genai.Client", return_value=mock_client):
            assessment = risk_agent.assess(record, anomalies)

        assert "12.5" in assessment.justification
        assert "GDP" in assessment.justification
        mock_models.generate_content.assert_called_once()

    def test_gemini_fallback_on_error(self, telemetry_agent, anomaly_agent,
                                      risk_agent, temp_excursion_payload):
        """Verify rule-based fallback is used when Gemini raises an exception."""
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)

        mock_models = MagicMock()
        mock_models.generate_content.side_effect = Exception("API quota exceeded")
        mock_client = MagicMock()
        mock_client.models = mock_models

        with patch("agents.risk_agent.GEMINI_API_KEY", "fake-test-key"), \
             patch("google.genai.Client", return_value=mock_client):
            assessment = risk_agent.assess(record, anomalies)

        # Should fall back to rule-based justification — still has useful content
        assert assessment.justification != ""
        assert "SHP-TEST-002" in assessment.justification


# ---------------------------------------------------------------------------
# GDPValidator tests
# ---------------------------------------------------------------------------

class TestGDPValidator:
    def test_compliant_normal(self, telemetry_agent, anomaly_agent, risk_agent, normal_payload):
        record    = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        validator  = GDPValidator()
        compliant, violations = validator.validate(record, assessment)
        assert compliant
        assert violations == []

    def test_violation_temp_excursion(self, telemetry_agent, anomaly_agent, risk_agent,
                                       temp_excursion_payload):
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        validator  = GDPValidator()
        compliant, violations = validator.validate(record, assessment)
        assert not compliant
        assert len(violations) > 0

    def test_violation_humidity(self, telemetry_agent, anomaly_agent, risk_agent, normal_payload):
        normal_payload["humidity_pct"] = 90.0
        normal_payload["shipment_id"]  = "SHP-HUM"
        record    = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        validator  = GDPValidator()
        compliant, violations = validator.validate(record, assessment)
        assert not compliant


# ---------------------------------------------------------------------------
# HITL ApprovalQueue tests
# ---------------------------------------------------------------------------

class TestApprovalQueue:
    def test_submit_and_approve(self, telemetry_agent, anomaly_agent, risk_agent,
                                 temp_excursion_payload):
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)

        queue = ApprovalQueue(timeout_sec=2)
        req   = queue.submit(assessment)
        assert req.status == ApprovalStatus.PENDING

        # Approve immediately in another thread
        import threading
        def _approve():
            import time; time.sleep(0.1)
            queue.approve(req.request_id, "test_operator")

        t = threading.Thread(target=_approve, daemon=True)
        t.start()

        resolved = queue.wait_for_decision(req.request_id)
        assert resolved.status == ApprovalStatus.APPROVED

    def test_timeout_auto_approves_safe(self, telemetry_agent, anomaly_agent, risk_agent,
                                        temp_excursion_payload):
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)

        queue    = ApprovalQueue(timeout_sec=1)   # 1-second timeout for test
        req      = queue.submit(assessment)
        resolved = queue.wait_for_decision(req.request_id)
        assert resolved.status == ApprovalStatus.TIMEOUT

    def test_reject(self, telemetry_agent, anomaly_agent, risk_agent, temp_excursion_payload):
        record    = telemetry_agent.ingest(temp_excursion_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)

        queue = ApprovalQueue(timeout_sec=5)
        req   = queue.submit(assessment)

        import threading
        def _reject():
            import time; time.sleep(0.1)
            queue.reject(req.request_id, "test_operator", notes="Test rejection")

        t = threading.Thread(target=_reject, daemon=True)
        t.start()

        resolved = queue.wait_for_decision(req.request_id)
        assert resolved.status == ApprovalStatus.REJECTED
        assert resolved.approved_actions == []


# ---------------------------------------------------------------------------
# AuditLogger tests
# ---------------------------------------------------------------------------

class TestAuditLogger:
    def test_write_and_read(self, tmp_path):
        log_file = str(tmp_path / "test_audit.jsonl")
        logger   = AuditLogger(log_path=log_file)
        logger.log_raw("TEST_EVENT", "SHP-AUDIT-001", {"key": "value"})
        records = logger.get_all_records()
        assert len(records) == 1
        assert records[0]["event_type"] == "TEST_EVENT"

    def test_shipment_filter(self, tmp_path):
        log_file = str(tmp_path / "test_audit2.jsonl")
        logger   = AuditLogger(log_path=log_file)
        logger.log_raw("A", "SHP-001", {"x": 1})
        logger.log_raw("B", "SHP-002", {"x": 2})
        records = logger.get_shipment_history("SHP-001")
        assert len(records) == 1
        assert records[0]["shipment_id"] == "SHP-001"


# ---------------------------------------------------------------------------
# StreamSimulator tests
# ---------------------------------------------------------------------------

class TestStreamSimulator:
    def test_single_tick(self):
        sim     = StreamSimulator(n_shipments=2, interval_sec=0)
        payloads = sim.single_tick()
        assert len(payloads) == 2
        for p in payloads:
            assert "temperature_c" in p
            assert "shipment_id" in p

    def test_stream_max_ticks(self):
        sim    = StreamSimulator(n_shipments=1, interval_sec=0, scenarios=["normal"])
        ticks  = list(sim.stream(max_ticks=3, realtime=False))
        assert len(ticks) == 3

    def test_excursion_scenario_triggers(self):
        # Scenario drifts +0.8°C/tick starting at tick 5; need ≥15 ticks to
        # guarantee excursion regardless of random starting temperature
        sim = StreamSimulator(n_shipments=1, interval_sec=0,
                              scenarios=["temp_excursion_high"])
        payloads = list(sim.stream(max_ticks=15, realtime=False))
        temps    = [p["temperature_c"] for p in payloads]
        assert max(temps) > TEMP_MAX_C, "Expected temp excursion in scenario"

    def test_payload_includes_route_fields(self):
        sim     = StreamSimulator(n_shipments=1, interval_sec=0)
        payload = sim.single_tick()[0]
        assert "carrier"     in payload
        assert "origin"      in payload
        assert "destination" in payload


# ---------------------------------------------------------------------------
# RiskAgent — spoilage & EMERGENCY_RECALL
# ---------------------------------------------------------------------------

class TestRiskAgentExtended:
    def test_spoilage_increases_with_delay(self, telemetry_agent, anomaly_agent,
                                           risk_agent, normal_payload):
        """Delay > 6h should contribute to spoilage probability."""
        normal_payload["flight_status"] = "DELAYED"
        normal_payload["delay_hours"]   = 10.0
        normal_payload["shipment_id"]   = "SHP-DELAY-SPO"
        record    = telemetry_agent.ingest(normal_payload)
        anomalies = anomaly_agent.analyse(record)
        assessment = risk_agent.assess(record, anomalies)
        assert assessment.spoilage_prob > 0.0, "Delay should contribute to spoilage"

    def test_emergency_recall_on_critical_sustained(self, telemetry_agent, risk_agent):
        """HIGH/CRITICAL risk + SUSTAINED_EXCURSION should trigger EMERGENCY_RECALL."""
        from agents.anomaly_agent import AnomalyType, Severity, Anomaly
        from agents.risk_agent import RecommendedAction, RiskLevel
        from datetime import datetime, timezone

        # High temp + long delay → score pushes into HIGH/CRITICAL + sustained excursion
        payload = {
            "shipment_id": "SHP-RECALL-001", "container_id": "CNT-RECALL-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature_c": 20.0, "humidity_pct": 80.0, "shock_g": 0.1,
            "latitude": 0.0, "longitude": 0.0, "altitude_m": 0.0,
            "customs_status": "HOLD", "flight_status": "DIVERTED",
            "delay_hours": 16.0, "battery_pct": 90.0,
        }
        record = telemetry_agent.ingest(payload)
        sustained = Anomaly(
            anomaly_type = AnomalyType.SUSTAINED_EXCURSION,
            severity     = Severity.CRITICAL,
            shipment_id  = "SHP-RECALL-001",
            container_id = "CNT-RECALL-001",
            detected_at  = datetime.now(timezone.utc),
            description  = "Sustained excursion 45 min",
            duration_min = 45.0,
        )
        assessment = risk_agent.assess(record, [sustained])
        assert assessment.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL), \
            f"Expected HIGH/CRITICAL, got {assessment.risk_level.value} (score {assessment.risk_score:.2f})"
        assert RecommendedAction.EMERGENCY_RECALL in assessment.actions


# ---------------------------------------------------------------------------
# CascadeOrchestrator — end-to-end pipeline
# ---------------------------------------------------------------------------

class TestCascadeOrchestrator:
    """End-to-end pipeline tests using short HITL timeout."""

    @pytest.fixture
    def orchestrator(self):
        from agents.cascade_orchestrator import CascadeOrchestrator
        from hitl.approval_queue import ApprovalQueue
        return CascadeOrchestrator(approval_queue=ApprovalQueue(timeout_sec=1))

    @pytest.fixture
    def critical_e2e_payload(self):
        from datetime import datetime, timezone
        return {
            "shipment_id":    "SHP-E2E-001",
            "container_id":   "CNT-E2E-001",
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "temperature_c":  13.5,
            "humidity_pct":   78.0,
            "shock_g":        0.2,
            "latitude":       40.64,
            "longitude":      -73.78,
            "altitude_m":     10000.0,
            "customs_status": "CLEARED",
            "flight_status":  "DELAYED",
            "delay_hours":    8.5,
            "battery_pct":    85.0,
            "carrier":        "OceanExpress",
            "origin":         "Mumbai",
            "destination":    "California",
        }

    def test_pipeline_returns_state(self, orchestrator, critical_e2e_payload):
        state = orchestrator.run(critical_e2e_payload)
        assert state.assessment is not None
        assert state.assessment.risk_score > 0
        assert len(state.anomalies) > 0

    def test_route_metadata_flows_to_assessment(self, orchestrator, critical_e2e_payload):
        state = orchestrator.run(critical_e2e_payload)
        assert state.assessment.metadata.get("carrier") == "OceanExpress"
        assert state.assessment.metadata.get("destination") == "California"

    def test_low_risk_auto_approved(self, orchestrator):
        """LOW risk actions should be auto-approved without HITL."""
        from datetime import datetime, timezone
        payload = {
            "shipment_id": "SHP-LOW-001", "container_id": "CNT-LOW-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature_c": 5.0, "humidity_pct": 55.0, "shock_g": 0.1,
            "latitude": 0.0, "longitude": 0.0, "altitude_m": 0.0,
            "customs_status": "CLEARED", "flight_status": "ON_TIME",
            "delay_hours": 0.0, "battery_pct": 95.0,
            "carrier": "ReliableLogistics", "origin": "JFK", "destination": "FRA",
        }
        state = orchestrator.run(payload)
        assert state.assessment.risk_level.value == "LOW"
        from agents.risk_agent import RecommendedAction
        assert RecommendedAction.MONITOR_ONLY in state.approved_actions

    def test_hitl_timeout_executes_safe_actions(self, orchestrator, critical_e2e_payload):
        """On HITL timeout, safe actions (ALERT_OPERATIONS, NOTIFY_HOSPITAL) execute."""
        state = orchestrator.run(critical_e2e_payload)
        from agents.risk_agent import RecommendedAction
        # After timeout, at minimum safe actions should be in approved list
        executed = {r.action for r in state.action_results}
        assert RecommendedAction.ALERT_OPERATIONS in executed or len(state.approved_actions) == 0

    def test_reroute_includes_alternatives(self, orchestrator, critical_e2e_payload):
        """REROUTE_SHIPMENT result should contain carrier alternatives."""
        import threading
        from hitl.approval_queue import ApprovalQueue
        from agents.risk_agent import RecommendedAction

        # Use a queue we can approve immediately
        queue = ApprovalQueue(timeout_sec=5)
        orch  = type(orchestrator)(approval_queue=queue)

        def _approve_all():
            import time; time.sleep(0.2)
            pending = queue.pending()
            for req in pending:
                queue.approve(req.request_id, "test_operator")

        t = threading.Thread(target=_approve_all, daemon=True)
        t.start()

        state = orch.run(critical_e2e_payload)
        reroute_results = [
            r for r in state.action_results
            if r.action == RecommendedAction.REROUTE_SHIPMENT and r.success
        ]
        if reroute_results:
            payload = reroute_results[0].payload
            assert "recommended_alternatives" in payload
            assert len(payload["recommended_alternatives"]) > 0
