"""
start.py
--------
One-command launcher for Pharma Cargo Monitor.
Starts the HITL dashboard + map simulation + live simulation together,
sharing a single in-memory ApprovalQueue so every agent
decision surfaces directly in the browser.

Usage:
    python start.py                          # defaults: 3 shipments, 40 ticks, port 8080
    python start.py --shipments 5 --ticks 60
    python start.py --port 3000
    python start.py --map-port 8090          # map simulation port (default: 8090)
    python start.py --no-browser             # skip auto-open
    python start.py --interval 3             # 3 seconds between ticks (default: 2)
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers= [logging.StreamHandler(sys.stdout)],
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger("pharma-cargo.start")

# ── banner ────────────────────────────────────────────────────────────────────
BANNER = """
╔══════════════════════════════════════════════════════════╗
║        PHARMA CARGO MONITOR  —  Agent Terps             ║
║        UMD Agentic AI Challenge 2026                     ║
╚══════════════════════════════════════════════════════════╝
"""


# ── dashboard thread ──────────────────────────────────────────────────────────

def _start_dashboard(queue, orchestrator, port: int) -> None:
    """
    Run the HITL dashboard (hitl/dashboard.py) in a daemon thread.
    Shares the caller's ApprovalQueue and CascadeOrchestrator so the
    dashboard /simulate endpoint and the CLI simulation both use the
    same in-memory state.
    """
    try:
        import uvicorn
    except ImportError:
        logger.error("uvicorn not installed — run: pip install uvicorn")
        return

    from hitl.dashboard import app, set_queue, set_orchestrator
    set_queue(queue)
    set_orchestrator(orchestrator)

    config = uvicorn.Config(
        app,
        host      = "0.0.0.0",
        port      = port,
        log_level = "warning",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = False  # main thread owns signals
    server.run()


# ── map simulation thread ─────────────────────────────────────────────────────

def _start_map_sim(queue, orchestrator, port: int) -> None:
    """
    Run the interactive map simulation (map_sim/app.py) in a daemon thread.
    Shares the same ApprovalQueue and CascadeOrchestrator as the dashboard
    so HITL decisions from the map appear in both UIs.
    """
    try:
        import uvicorn
    except ImportError:
        logger.error("uvicorn not installed — run: pip install uvicorn")
        return

    from map_sim.app import app as map_app, set_queue as map_set_queue, set_orchestrator as map_set_orch
    map_set_queue(queue)
    map_set_orch(orchestrator)

    config = uvicorn.Config(
        map_app,
        host      = "0.0.0.0",
        port      = port,
        log_level = "warning",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = False
    server.run()


# ── simulation ────────────────────────────────────────────────────────────────

def _build_orchestrator(queue):
    """Construct the full agent pipeline wired to the shared queue."""
    from agents.telemetry_agent           import TelemetryAgent
    from agents.anomaly_agent             import AnomalyAgent
    from agents.risk_agent                import RiskAgent
    from agents.action_agent              import ActionAgent
    from agents.cascade_orchestrator      import CascadeOrchestrator
    from compliance.audit_logger          import AuditLogger
    from compliance.gdp_rules             import GDPValidator
    from notifications.hospital_notifier  import HospitalNotifier
    from notifications.insurance_docs     import InsuranceDocGenerator
    from notifications.inventory_updater  import InventoryUpdater

    tel = TelemetryAgent()
    return CascadeOrchestrator(
        telemetry_agent   = tel,
        anomaly_agent     = AnomalyAgent(tel),
        risk_agent        = RiskAgent(),
        action_agent      = ActionAgent(),
        approval_queue    = queue,          # ← shared with dashboard + map sim
        audit_logger      = AuditLogger(),
        gdp_validator     = GDPValidator(),
        hospital_notifier = HospitalNotifier(),
        insurance_docs    = InsuranceDocGenerator(),
        inventory_updater = InventoryUpdater(),
    )


def _run_simulation(orchestrator, n_shipments: int, max_ticks: int, interval: float) -> None:
    """Stream telemetry through the pipeline — results surface live in the dashboard."""
    from data.dataset_loader import loader as dataset_loader
    logger.info("\n%s", dataset_loader.calibration_summary())

    from simulation.stream_simulator import StreamSimulator

    sim = StreamSimulator(n_shipments=n_shipments, interval_sec=interval)
    logger.info(
        "Streaming %d shipments x %d ticks  (%.1fs interval) ...",
        n_shipments, max_ticks, interval,
    )

    for i, payload in enumerate(sim.stream(max_ticks=max_ticks, realtime=True)):
        logger.info(
            "─── Tick %-3d │ %-12s │ carrier=%-16s │ %s → %s",
            i + 1,
            payload["shipment_id"],
            payload.get("carrier", "?"),
            payload.get("origin", "?"),
            payload.get("destination", "?"),
        )
        state = orchestrator.run(payload)
        if state.assessment:
            level = state.assessment.risk_level.value
            score = state.assessment.risk_score
            flag  = "[!!]" if level == "CRITICAL" else "[! ]" if level == "HIGH" else "[~ ]" if level == "MEDIUM" else "[ok]"
            logger.info(
                "  %s  Risk=%-8s Score=%.2f  Anomalies=%-2d  Actions=%s",
                flag, level, score,
                len(state.anomalies),
                [a.value for a in state.approved_actions],
            )
        if state.errors:
            logger.warning("  Errors: %s", state.errors)


# ── entry point ───────────────────────────────────────────────────────────────

def _wait_for_port(port: int, timeout_sec: float = 5.0) -> bool:
    """Poll until port is open or timeout expires. Returns True if open."""
    import socket
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) == 0:
                return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pharma Cargo Monitor — one-command launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--shipments",  type=int,   default=3,    help="Concurrent shipments to simulate")
    parser.add_argument("--ticks",      type=int,   default=40,   help="Telemetry ticks per shipment")
    parser.add_argument("--port",       type=int,   default=8080, help="HITL dashboard port")
    parser.add_argument("--map-port",   type=int,   default=8090, help="Map simulation port")
    parser.add_argument("--interval",   type=float, default=2.0,  help="Seconds between telemetry ticks")
    parser.add_argument("--no-browser", action="store_true",      help="Skip auto-opening the browser")
    args = parser.parse_args()

    print(BANNER)

    import socket

    # ── check both ports are free ─────────────────────────────────────────────
    for port, label in [(args.port, "Dashboard"), (args.map_port, "Map simulation")]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) == 0:
                logger.error(
                    "%s port %d is already in use. Stop the existing process or use --%s <other>",
                    label, port, "port" if port == args.port else "map-port",
                )
                sys.exit(1)

    # ── shared queue + orchestrator (wires dashboard ↔ map sim ↔ simulation) ──
    from hitl.approval_queue import ApprovalQueue
    shared_queue        = ApprovalQueue(timeout_sec=300)
    logger.info("Building agent pipeline ...")
    shared_orchestrator = _build_orchestrator(shared_queue)

    # ── start HITL dashboard in background thread ─────────────────────────────
    dash_thread = threading.Thread(
        target = _start_dashboard,
        args   = (shared_queue, shared_orchestrator, args.port),
        daemon = True,
        name   = "hitl-dashboard",
    )
    dash_thread.start()
    logger.info("HITL dashboard starting on port %d ...", args.port)

    # ── start map simulation in background thread ─────────────────────────────
    map_thread = threading.Thread(
        target = _start_map_sim,
        args   = (shared_queue, shared_orchestrator, args.map_port),
        daemon = True,
        name   = "map-simulation",
    )
    map_thread.start()
    logger.info("Map simulation starting on port %d ...", args.map_port)

    # ── wait for both servers to bind ─────────────────────────────────────────
    dash_url = f"http://localhost:{args.port}"
    map_url  = f"http://localhost:{args.map_port}"

    if not _wait_for_port(args.port):
        logger.warning("HITL dashboard did not respond in 5 s — continuing anyway")
    if not _wait_for_port(args.map_port):
        logger.warning("Map simulation did not respond in 5 s — continuing anyway")

    # ── print startup summary ─────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("  [OK] HITL Dashboard  : %s", dash_url)
    logger.info("  [OK] Map Simulation  : %s", map_url)
    logger.info("  [OK] Shipments : %d   Ticks : %d   Interval : %.1fs",
                args.shipments, args.ticks, args.interval)
    logger.info("  [OK] Audit log : data/processed/audit.jsonl")
    logger.info("=" * 60)

    # ── open browser ─────────────────────────────────────────────────────────
    if not args.no_browser:
        webbrowser.open(dash_url)
        webbrowser.open(map_url)
        logger.info("Browser opened → %s  and  %s", dash_url, map_url)

    # ── run simulation in main thread ─────────────────────────────────────────
    try:
        _run_simulation(shared_orchestrator, args.shipments, args.ticks, args.interval)
    except KeyboardInterrupt:
        logger.info("Simulation interrupted.")

    # ── keep both services alive after simulation finishes ────────────────────
    logger.info("")
    logger.info("Simulation complete. Services still live:")
    logger.info("  HITL Dashboard : %s", dash_url)
    logger.info("  Map Simulation : %s", map_url)
    logger.info("Press Ctrl+C to shut down.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down. Goodbye.")


if __name__ == "__main__":
    main()
