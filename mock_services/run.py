"""
Run mock notification services on ports 9001, 9002, 9003.

Usage:
    python -m mock_services.run

This starts three uvicorn instances in background threads so all
downstream notification endpoints (hospital, inventory, insurance)
return real responses instead of "dry_run".
"""

from __future__ import annotations

import threading
import time
import uvicorn


def _run_on_port(port: int) -> None:
    uvicorn.run(
        "mock_services.server:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


def start_all() -> None:
    """Start mock services on ports 9001, 9002, 9003 in background threads."""
    ports = {
        9001: "hospital",
        9002: "inventory",
        9003: "insurance",
    }
    for port, name in ports.items():
        t = threading.Thread(target=_run_on_port, args=(port,), daemon=True)
        t.start()
        print(f"  ✓ Mock {name} service on http://localhost:{port}")


if __name__ == "__main__":
    print("Starting mock notification services...")
    start_all()
    print("\nAll mock services running. Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
