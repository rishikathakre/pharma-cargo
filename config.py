"""
Central configuration for Pharma Cargo Monitor.
All thresholds, API keys, and system parameters live here.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

# Load .env if present (no external dependency needed)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Telemetry thresholds (GDP / cold-chain defaults)
# ---------------------------------------------------------------------------
TEMP_MIN_C = 2.0          # °C  – lower safe bound (cold-chain vaccines)
TEMP_MAX_C = 8.0          # °C  – upper safe bound
HUMIDITY_MAX_PCT = 75.0   # %RH – maximum acceptable humidity
SHOCK_MAX_G = 3.0         # g   – maximum acceptable shock
EXCURSION_MINUTES = 30    # minutes before temp excursion triggers escalation

# ---------------------------------------------------------------------------
# Risk scoring weights
# ---------------------------------------------------------------------------
RISK_WEIGHTS: Dict[str, float] = {
    "temperature": 0.40,
    "humidity":    0.20,
    "shock":       0.15,
    "delay_hours": 0.15,
    "customs":     0.10,
}
RISK_HIGH_THRESHOLD   = 0.70   # 0-1 score above which HITL approval required
RISK_MEDIUM_THRESHOLD = 0.40

# ---------------------------------------------------------------------------
# LangGraph / LLM settings
# ---------------------------------------------------------------------------
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
LLM_MODEL       = "gemini-2.0-flash"
LLM_TEMPERATURE = 0.2

# ---------------------------------------------------------------------------
# Human-in-the-loop settings
# ---------------------------------------------------------------------------
HITL_APPROVAL_TIMEOUT_SEC = 300   # seconds before auto-escalation
HITL_AUTO_APPROVE_LOW     = True  # auto-approve LOW-risk actions

# ---------------------------------------------------------------------------
# Notification endpoints (override via env vars in production)
# ---------------------------------------------------------------------------
HOSPITAL_WEBHOOK_URL   = os.getenv("HOSPITAL_WEBHOOK_URL",  "http://localhost:9001/notify")
INVENTORY_API_URL      = os.getenv("INVENTORY_API_URL",     "http://localhost:9002/inventory")
INSURANCE_API_URL      = os.getenv("INSURANCE_API_URL",     "http://localhost:9003/claims")
CUSTOMS_API_URL        = os.getenv("CUSTOMS_API_URL",       "http://localhost:9004/customs")

# ---------------------------------------------------------------------------
# Compliance / audit
# ---------------------------------------------------------------------------
AUDIT_LOG_PATH   = os.getenv("AUDIT_LOG_PATH", "data/processed/audit.jsonl")
GDP_REGULATION   = "EU GDP 2013/C 343/01"
FDA_REGULATION   = "21 CFR Part 211 / 600"

# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
SIMULATION_INTERVAL_SEC = 5     # seconds between synthetic telemetry ticks
SIMULATION_SHIPMENTS    = 3     # number of concurrent simulated shipments
