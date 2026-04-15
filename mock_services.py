"""
mock_services.py
----------------
Tiny local HTTP services used for demo/prototype integrations.

Starts three endpoints:
  - Hospital webhook  : http://localhost:9001/notify
  - Inventory API     : http://localhost:9002/inventory
  - Insurance API     : http://localhost:9003/claims

Each service logs received JSON and responds with {"status":"received"}.
This eliminates "dry_run" outputs during the demo.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


class _JsonHandler(BaseHTTPRequestHandler):
    server_version = "PharmaCargoMock/1.0"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""

        payload = None
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            payload = {"_raw": body.decode("utf-8", errors="replace")}

        name = getattr(self.server, "service_name", "mock-service")
        logger.info("[%s] POST %s payload=%s", name, self.path, str(payload)[:500])

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "received", "service": name}).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        name = getattr(self.server, "service_name", "mock-service")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "service": name}).encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002
        # Silence default http.server access logs; we log structured above.
        return


def _serve(service_name: str, host: str, port: int) -> None:
    httpd = HTTPServer((host, port), _JsonHandler)
    httpd.service_name = service_name  # type: ignore[attr-defined]
    logger.info("[%s] listening on http://%s:%d", service_name, host, port)
    httpd.serve_forever()


def start_mock_services(
    host: str = "127.0.0.1",
    ports: Dict[str, int] | None = None,
) -> Dict[str, Tuple[str, int]]:
    """
    Start demo mock services in background threads.
    Returns mapping of service_name -> (host, port).
    """
    ports = ports or {"hospital": 9001, "inventory": 9002, "insurance": 9003}
    endpoints: Dict[str, Tuple[str, int]] = {}

    for name, port in ports.items():
        t = threading.Thread(target=_serve, args=(name, host, port), daemon=True)
        t.start()
        endpoints[name] = (host, port)

    return endpoints

