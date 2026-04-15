"""
main.py
-------
Entry point for the Pharma Cargo Monitor system.

Modes:
  python main.py simulate          – run full simulation with all agents
  python main.py dashboard         – start HITL FastAPI dashboard
  python main.py test-pipeline     – run a single pipeline cycle (smoke test)
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers = [logging.StreamHandler(sys.stdout)],
)
# Ensure Unicode output works on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logger = logging.getLogger("pharma-cargo.main")


def run_simulation(n_shipments: int = 3, max_ticks: int = 20) -> None:
    """Run the full agentic pipeline against simulated telemetry."""
    from data.dataset_loader import loader as dataset_loader
    logger.info("\n%s", dataset_loader.calibration_summary())

    # Start local mock integrations for a clean demo (no dry_run).
    try:
        from mock_services import start_mock_services
        start_mock_services()
    except Exception as exc:
        logger.warning("Mock services not started: %s", exc)

    from agents.telemetry_agent   import TelemetryAgent
    from agents.anomaly_agent     import AnomalyAgent
    from agents.risk_agent        import RiskAgent
    from agents.action_agent      import ActionAgent
    from agents.cascade_orchestrator import CascadeOrchestrator
    from hitl.approval_queue      import ApprovalQueue
    from compliance.audit_logger  import AuditLogger
    from compliance.gdp_rules     import GDPValidator
    from notifications.hospital_notifier import HospitalNotifier
    from notifications.insurance_docs    import InsuranceDocGenerator
    from notifications.inventory_updater import InventoryUpdater
    from simulation.stream_simulator     import StreamSimulator

    logger.info("Initialising agents…")

    tel   = TelemetryAgent()
    ano   = AnomalyAgent(tel)
    risk  = RiskAgent()
    act   = ActionAgent()
    queue = ApprovalQueue(timeout_sec=10)   # short timeout for demo
    audit = AuditLogger()
    gdp   = GDPValidator()
    hosp  = HospitalNotifier()
    ins   = InsuranceDocGenerator()
    inv   = InventoryUpdater()

    orchestrator = CascadeOrchestrator(
        telemetry_agent   = tel,
        anomaly_agent     = ano,
        risk_agent        = risk,
        action_agent      = act,
        approval_queue    = queue,
        audit_logger      = audit,
        gdp_validator     = gdp,
        hospital_notifier = hosp,
        insurance_docs    = ins,
        inventory_updater = inv,
    )

    sim = StreamSimulator(n_shipments=n_shipments, interval_sec=0)
    logger.info("Starting simulation: %d shipments, %d ticks each",
                n_shipments, max_ticks)

    for i, payload in enumerate(sim.stream(max_ticks=max_ticks, realtime=False)):
        logger.info("─── Tick %d | Shipment %s ───", i + 1, payload["shipment_id"])
        state = orchestrator.run(payload)
        logger.info(
            "  Risk=%-8s Score=%.2f  Anomalies=%-2d  Actions=%s  Compliant=%s",
            state.assessment.risk_level.value if state.assessment else "N/A",
            state.assessment.risk_score if state.assessment else 0.0,
            len(state.anomalies),
            [a.value for a in state.approved_actions],
            state.compliant,
        )
        if state.errors:
            logger.warning("  Errors: %s", state.errors)


def run_single_pipeline() -> None:
    """Quick smoke-test: inject one critical payload and run the pipeline."""
    from agents.cascade_orchestrator import CascadeOrchestrator
    from datetime import datetime, timezone

    from hitl.approval_queue import ApprovalQueue
    orchestrator = CascadeOrchestrator(
        approval_queue=ApprovalQueue(timeout_sec=5)  # short timeout for smoke test
    )

    payload = {
        "shipment_id":    "SHP-SMOKE-001",
        "container_id":   "CNT-SMOKE-001",
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "temperature_c":  12.8,
        "humidity_pct":   80.0,
        "shock_g":        0.2,
        "latitude":       40.64,
        "longitude":      -73.78,
        "altitude_m":     10000.0,
        "customs_status": "CLEARED",
        "flight_status":  "DELAYED",
        "delay_hours":    7.5,
        "battery_pct":    85.0,
        "carrier":        "OceanExpress",
        "origin":         "Mumbai",
        "destination":    "California",
    }

    logger.info("Running single pipeline cycle…")
    state = orchestrator.run(payload)
    logger.info("Pipeline complete.")
    logger.info("  Risk level  : %s", state.assessment.risk_level.value if state.assessment else "N/A")
    logger.info("  Risk score  : %.2f", state.assessment.risk_score if state.assessment else 0.0)
    logger.info("  Anomalies   : %d", len(state.anomalies))
    logger.info("  Actions     : %s", [a.value for a in state.approved_actions])
    logger.info("  Compliant   : %s", state.compliant)
    if state.errors:
        logger.warning("  Errors      : %s", state.errors)


def run_dashboard(port: int = 8080) -> None:
    """Start the HITL FastAPI dashboard."""
    try:
        import uvicorn
    except ImportError:
        logger.error("uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    from hitl.approval_queue import ApprovalQueue
    from hitl.dashboard import app, set_queue, set_orchestrator
    from agents.cascade_orchestrator import CascadeOrchestrator

    queue = ApprovalQueue()
    set_queue(queue)
    # Orchestrator shares the same in-memory ApprovalQueue so the UI can
    # show pending approvals from simulation runs started via the dashboard.
    set_orchestrator(CascadeOrchestrator(approval_queue=queue))

    logger.info("Starting HITL dashboard on http://localhost:%d", port)
    logger.info("API docs: http://localhost:%d/docs", port)
    uvicorn.run(app, host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pharma Cargo Monitor – Agentic AI System"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # simulate
    sim_parser = subparsers.add_parser("simulate", help="Run full simulation")
    sim_parser.add_argument("--shipments", type=int, default=3)
    sim_parser.add_argument("--ticks",     type=int, default=20)

    # dashboard
    dash_parser = subparsers.add_parser("dashboard", help="Start HITL dashboard")
    dash_parser.add_argument("--port", type=int, default=8080)

    # test-pipeline
    subparsers.add_parser("test-pipeline", help="Single pipeline smoke test")

    args = parser.parse_args()

    if args.command == "simulate":
        run_simulation(n_shipments=args.shipments, max_ticks=args.ticks)
    elif args.command == "dashboard":
        run_dashboard(port=args.port)
    elif args.command == "test-pipeline":
        run_single_pipeline()


if __name__ == "__main__":
    main()
