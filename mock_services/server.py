"""
mock_services/server.py
-----------------------
Tiny FastAPI app that acts as a mock for all downstream notification services.
Logs received payloads and returns {"status": "received"}.

Run:
    uvicorn mock_services.server:app --port 9001

Or start all three ports programmatically via start_all().
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("mock-services")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# In-memory log of all received requests (for inspection / demo)
_received: List[Dict[str, Any]] = []

app = FastAPI(title="Pharma Cargo – Mock Services", version="1.0.0")


@app.post("/{path:path}")
async def catch_all_post(path: str, request: Request) -> JSONResponse:
    """Accept any POST, log it, return acknowledgement."""
    try:
        body = await request.json()
    except Exception:
        body = {"_raw": (await request.body()).decode("utf-8", errors="replace")}

    shipment_id = body.get("shipment_id", "?")
    action = body.get("action") or body.get("notification_type") or path

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "path": f"/{path}",
        "shipment_id": shipment_id,
        "action": action,
        "payload_keys": list(body.keys()),
    }
    _received.append(entry)

    logger.info(
        "[MOCK] POST /%s | shipment=%s | action=%s | keys=%s",
        path, shipment_id, action, list(body.keys()),
    )

    return JSONResponse({"status": "received", "path": f"/{path}", "shipment_id": shipment_id})


@app.get("/{path:path}")
async def catch_all_get(path: str) -> JSONResponse:
    """Health check / inspection."""
    return JSONResponse({"status": "ok", "path": f"/{path}", "total_received": len(_received)})


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "pharma-cargo-mock-services",
        "total_received": len(_received),
        "recent": _received[-10:] if _received else [],
    })
