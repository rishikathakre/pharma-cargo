# Pharma Cargo Monitor
### UMD Agentic AI Challenge 2026 — Cargo Monitoring Track

An agentic AI system for real-time pharmaceutical cold-chain monitoring. Autonomous agents detect anomalies in IoT telemetry from smart containers, assess risk of spoilage or delay, and trigger cascading operational actions — all with GDP/FDA-compliant audit trails and human-in-the-loop oversight.

---

## Architecture

```
IoT Telemetry (sensors)
        │
        ▼
┌─────────────────┐
│ TelemetryAgent  │  Parse & normalise raw sensor payloads
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  AnomalyAgent   │  Rule-based anomaly detection
│                 │  (temp, humidity, shock, customs, delays)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   RiskAgent     │  Weighted risk scoring (0-1)
│                 │  + action recommendation
└────────┬────────┘
         │
         ├──── LOW risk ──────────────────────────────────────────────┐
         │                                                            │
         ▼                                                            │
┌─────────────────┐                                                   │
│  GDPValidator   │  GDP/FDA compliance check                         │
└────────┬────────┘                                                   │
         │                                                            │
         ▼                                                            │
┌─────────────────┐     ┌──────────────────┐                         │
│   HITL Gate     │────▶│ HITL Dashboard   │ Operator approve/reject  │
│ (ApprovalQueue) │     │  (FastAPI REST)  │                         │
└────────┬────────┘     └──────────────────┘                         │
         │                                                            │
         ▼                                                            │
┌─────────────────┐◀───────────────────────────────────────────────────┘
│  ActionAgent    │  Execute approved cascading actions
└────────┬────────┘
         │
    ┌────┴──────────────────────────────────────────────┐
    │           Cascading Actions                        │
    ├── HospitalNotifier      (patient reschedule alerts)│
    ├── InsuranceDocGenerator (claim initiation)         │
    ├── InventoryUpdater      (cold-storage / quarantine)│
    ├── Rerouting             (alternative carrier/route)│
    └── CustomsEscalation     (priority processing)      │
         │
         ▼
┌─────────────────┐
│  AuditLogger    │  Immutable JSONL audit trail (ALCOA+)
└─────────────────┘
```

**Orchestration**: LangGraph `StateGraph` wires all nodes into a directed pipeline with conditional routing.

---

## Project Structure

```
pharma-cargo/
├── agents/
│   ├── telemetry_agent.py       # IoT payload ingestion & normalisation
│   ├── anomaly_agent.py         # Anomaly detection (temp/humidity/shock/customs)
│   ├── risk_agent.py            # Risk scoring & action recommendation
│   ├── action_agent.py          # Action dispatcher
│   └── cascade_orchestrator.py  # LangGraph pipeline orchestration
├── hitl/
│   ├── approval_queue.py        # Thread-safe human-in-the-loop queue
│   └── dashboard.py             # FastAPI REST dashboard for operators
├── compliance/
│   ├── audit_logger.py          # Append-only JSONL audit trail
│   └── gdp_rules.py             # GDP / 21 CFR validation rules
├── notifications/
│   ├── hospital_notifier.py     # Healthcare provider alerts
│   ├── insurance_docs.py        # Automated insurance claim documents
│   └── inventory_updater.py     # Downstream inventory / cold-storage
├── simulation/
│   └── stream_simulator.py      # Synthetic telemetry generator (9 scenarios)
├── data/
│   ├── raw/                     # Raw incoming telemetry
│   ├── synthetic/               # Simulated datasets
│   └── processed/               # Audit logs & processed outputs
├── tests/
│   └── test_agents.py           # pytest suite (40+ test cases)
├── config.py                    # Centralised configuration & thresholds
├── main.py                      # CLI entry point
└── requirements.txt
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run a smoke test (single pipeline cycle)

```bash
python main.py test-pipeline
```

### 3. Run full simulation (3 shipments, 20 ticks each)

```bash
python main.py simulate --shipments 3 --ticks 20
```

### 4. Start the HITL operator dashboard

```bash
python main.py dashboard --port 8080
# API docs: http://localhost:8080/docs
```

### 5. Run tests

```bash
pytest tests/ -v
```

---

## Key Features

| Feature | Implementation |
|---|---|
| Real-time telemetry ingestion | `TelemetryAgent` with rolling per-shipment history |
| Multi-factor anomaly detection | Temperature (high/low/sustained), humidity, shock, customs hold, flight delay/diversion, battery |
| Weighted risk scoring | Configurable per-factor weights → 0-1 score + CRITICAL/HIGH/MEDIUM/LOW levels |
| Cascading action dispatch | Hospital alerts, insurance docs, inventory updates, rerouting, customs escalation |
| Human-in-the-loop | Thread-safe `ApprovalQueue` with timeout, partial approval, and FastAPI REST dashboard |
| GDP/FDA compliance | `GDPValidator` checks against EU GDP 2013/C 343/01 and 21 CFR Part 211/600 |
| Audit trail | Append-only JSONL log (ALCOA+), queryable by shipment ID or event type |
| Simulation | 9 anomaly scenarios, multi-shipment concurrent streaming |
| LangGraph orchestration | Typed `StateGraph` with conditional routing |

---

## Regulatory Compliance

- **EU GDP 2013/C 343/01** — temperature conditions, transit duration, import operations
- **21 CFR Part 211.68** — record retention (5 years)
- **21 CFR 600.15** — biological product temperature during shipment
- **ALCOA+** — attributable, legible, contemporaneous, original, accurate audit records

---

## Configuration

All thresholds and system parameters are in `config.py`:

```python
TEMP_MIN_C = 2.0          # °C lower cold-chain bound
TEMP_MAX_C = 8.0          # °C upper cold-chain bound
HUMIDITY_MAX_PCT = 75.0   # %RH maximum
SHOCK_MAX_G = 3.0         # g maximum shock
EXCURSION_MINUTES = 30    # sustained excursion trigger
RISK_HIGH_THRESHOLD = 0.70
HITL_APPROVAL_TIMEOUT_SEC = 300
```

Set `ANTHROPIC_API_KEY` in your environment to enable LLM-generated justifications.

---

## Team

UMD Smith School of Business — Agentic AI Challenge 2026
