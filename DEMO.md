# Pharma Cargo Monitor — Judge Demo Script
**UMD Agentic AI Challenge 2026 | Team Agent Terps**

> **How to use this doc:** Each section is one beat of the live demo.
> Read the bold presenter cues aloud. Point at what is described under "What you see."
> Total demo time: ~8 minutes.

---

## Pre-Demo Checklist (do this before judges arrive)

- [ ] Two terminal windows open, both `cd` into `pharma-cargo/`
- [ ] Browser open to a blank tab
- [ ] `.env` file present with `GEMINI_API_KEY=` (blank = rule-based fallback, still fully functional)
- [ ] `pip install -r requirements.txt` already done
- [ ] Run `pytest tests/ -v` once — confirm **39 passed** in terminal

---

## BEAT 1 — Show the pipeline cold (30 seconds)

### Terminal 1 — type and run:
```
python main.py test-pipeline
```

### What you see:
```
INFO  Running single pipeline cycle…
INFO  Risk level  : HIGH
INFO  Risk score  : 0.73
INFO  Anomalies   : 3
INFO  Actions     : ['NOTIFY_HOSPITAL', 'NOTIFY_INSURANCE', 'REQUEST_COLD_STORAGE']
INFO  Compliant   : True
```

### Presenter says:
> "This is a pre-loaded shipment — **SHP-SMOKE-001** — flying from **Mumbai to California**
> carrying refrigerated vaccines. The system ingested the sensor payload, ran it through
> six LangGraph nodes in under a second, detected three simultaneous anomalies —
> temperature at 12.8°C against a 2–8°C cold-chain limit, 7.5-hour flight delay,
> and humidity breach — scored it HIGH risk at 0.73, validated GDP compliance,
> and recommended three cascading actions. No human touched this."

**Point at:** `Risk level : HIGH` and `Actions :` line in the terminal.

---

## BEAT 2 — Open the HITL Dashboard (20 seconds)

### Terminal 1 — run:
```
python main.py dashboard
```

### Browser — navigate to:
```
http://localhost:8080
```

### What you see:
A live operator dashboard with:
- Purple gradient navbar: **Pharma Cargo Monitor — HITL Dashboard** + pulsing green LIVE dot
- Five stat cards across the top: **Pending / Approved / Rejected / Timed Out / Audit Events**
- Three empty panels: Pending Approvals, Resolved Decisions, Audit Log

### Presenter says:
> "This is the Human-in-the-Loop operator screen. Right now there is nothing queued —
> the system is idle. We're about to stream live telemetry into it."

---

## BEAT 3 — Start the Mumbai → California shipment stream (20 seconds)

### Terminal 2 — run:
```
python main.py simulate --shipments 3 --ticks 30
```

### What you see in Terminal 2 (immediately):
```
INFO  Starting simulation: 3 shipments, 30 ticks each
INFO  Dataset Calibration Summary:
INFO    Carrier reliability loaded: 8 carriers
INFO    Shipment routes loaded: 1000 (340 delayed)
INFO  ─── Tick 1 | Shipment SHP-1000 ───
INFO    Risk=LOW      Score=0.12  Anomalies=0   Actions=[]  Compliant=True
INFO  ─── Tick 1 | Shipment SHP-1001 ───
INFO    Risk=HIGH     Score=0.71  Anomalies=2   Actions=['NOTIFY_HOSPITAL', ...]
INFO  ─── Tick 1 | Shipment SHP-1002 ───
INFO    Risk=MEDIUM   Score=0.44  Anomalies=1   Actions=['LOG_ONLY']
```

### Presenter says:
> "Three concurrent shipments now streaming. Each line is one IoT sensor tick —
> temperature, humidity, shock, GPS, flight status — processed through the full
> agentic pipeline. Watch the dashboard."

**Switch to browser.**

### What you see in the browser (within 3 seconds):
- **Pending** stat card changes from `—` to a number
- An approval card appears in the **Pending Approvals** panel
- Card header is color-coded: orange border = HIGH, red border = CRITICAL

---

## BEAT 4 — The Storm Event / Flight Diversion (20 seconds)

### What to watch in Terminal 2 around Tick 5:
```
INFO  ─── Tick 5 | Shipment SHP-1001 ───
INFO    Risk=CRITICAL  Score=0.87  Anomalies=3   Actions=['NOTIFY_HOSPITAL',
         'NOTIFY_INSURANCE', 'REQUEST_COLD_STORAGE', 'ESCALATE_CUSTOMS']
```

The `flight_diversion` scenario triggers at tick 5 — this is the storm event.
The shipment's flight status flips to `DIVERTED` with 14-hour delay added.

### What you see in the browser:
- A new card appears at the **top** of Pending Approvals (CRITICAL sorts above HIGH)
- Card header has a **red left border** and rose-tinted background
- Risk badge shows **CRITICAL** in red
- Score shows `0.87` or higher
- Justification block (blue left accent) reads something like:
  > *"Shipment SHP-1001 is at critical risk. Flight diverted with 14h delay. Temperature
  >  at 12.1°C exceeds 8°C cold-chain limit for VACC-STANDARD. Spoilage probability: 0.61.
  >  Immediate cold-storage intervention required."*

### Presenter says:
> "Tick 5 — storm diversion. The flight status flipped to DIVERTED, delay jumped to
> 14 hours. The AI re-scored this CRITICAL at 0.87. Four cascading actions are now
> waiting for human sign-off. This card appeared automatically — no refresh button,
> no manual query. The agent pushed it here."

**Point at:** the red-bordered card at the top of the queue.

---

## BEAT 5 — Human Approves the Actions (40 seconds)

### In the browser, on the CRITICAL card:

1. **Point at the action checkboxes** — each proposed action is pre-checked:
   - ☑ NOTIFY_HOSPITAL
   - ☑ NOTIFY_INSURANCE
   - ☑ REQUEST_COLD_STORAGE
   - ☑ ESCALATE_CUSTOMS

2. **Uncheck ESCALATE_CUSTOMS** (demonstrate partial approval)
   > "We'll approve three of the four — customs escalation goes through a separate
   > compliance channel, so we deselect it here."

3. **Type in the Operator name field:** `Dr. Patel`

4. **Type in Notes:** `Confirmed storm diversion BOM-LAX sector`

5. **Click the green Approve button**

### What you see immediately:
- The CRITICAL card **disappears** from Pending Approvals
- It reappears in the **Resolved Decisions** table with status badge `PARTIAL` (blue)
- Decided By column shows: `Dr. Patel`
- Approved Actions column shows: `NOTIFY HOSPITAL, NOTIFY INSURANCE, REQUEST COLD STORAGE`
- **Pending** stat decrements by 1, **Approved** increments by 1

### What you see in Terminal 2:
```
INFO  [SHP-1001] HITL approved (PARTIAL) by Dr. Patel
INFO  [SHP-1001] → HospitalNotifier sent to 3 high-priority locations
INFO  [SHP-1001] → InsuranceDocGenerator: claim INS-2026-... initiated
INFO  [SHP-1001] → InventoryUpdater: cold-storage request sent (urgency=CRITICAL)
INFO  [SHP-1001] → doses_at_risk=3050  value_at_risk_usd=$54,900
```

### Presenter says:
> "Human approved in under 30 seconds. Three actions dispatched in parallel —
> hospitals along the California delivery route notified, insurance claim opened,
> cold-storage slot requested near the diversion airport.
> ESCALATE_CUSTOMS was intentionally withheld — partial approval is a first-class
> feature, not a workaround. The agent only executed what the operator signed off on."

---

## BEAT 6 — Point at the Audit Trail (30 seconds)

### In the browser, scroll down to the Audit Log panel:

- Dropdown is set to **Last 50 records**
- Change the **Event Type** filter to `HITL_DECISION`

### What you see:
One row appears:
```
Timestamp    │ HITL_DECISION │ SHP-1001 │ { "status": "PARTIAL",
                                           "decided_by": "Dr. Patel",
                                           "approved_actions": [...],
                                           "notes": "Confirmed storm diversion..." }
```

### Presenter says:
> "Every event — the pipeline run, the anomaly detection, the risk score, the HITL
> decision, each action result — is written to an append-only JSONL audit log.
> This record is immutable. It carries the operator's name, timestamp, exact actions
> approved, and the justification. This is ALCOA+ compliant — required under
> EU GDP §8 and 21 CFR 211.68 for pharmaceutical record retention."

**Change filter back to "All event types"** — show the full cascade of events for SHP-1001.

---

## BEAT 7 — Point at Cascade Output (20 seconds)

### Back in Terminal 2, scroll up to find SHP-1001 entries:

```
[SHP-1001] Cold-storage request → status: dry_run  (no live API — expected)
[SHP-1001] Hospital alert sent  → 3 CRITICAL-tier locations (CA vaccination demand)
[SHP-1001] Insurance claim      → INS-2026-XXXXXX  spoilage_prob=0.61
[SHP-1001] Inventory forecast   → adjustment=REDUCE  doses_at_risk=3050
[SHP-1001] Compliance check     → GDP §9.2 violation logged (excursion > threshold)
```

### Presenter says:
> "Five downstream systems touched from one human click.
> Hospital priority is driven by real US vaccination demand data —
> California ranks CRITICAL-tier because of its vaccination volumes.
> The insurance claim references GDP §9.2 and 21 CFR 211.142.
> The inventory system tells downstream clinics to reduce stock projections.
> The cold-storage request would hit the live logistics API in production —
> here it runs in dry-run mode since we're not wired to a live warehouse network."

---

## BEAT 8 — Close with the Test Suite (20 seconds)

### Show (already ran before demo):
```
pytest tests/ -v
```

```
tests/test_agents.py::test_telemetry_parse              PASSED
tests/test_agents.py::test_anomaly_temp_high            PASSED
tests/test_agents.py::test_risk_critical_score          PASSED
tests/test_agents.py::test_hitl_approve                 PASSED
tests/test_agents.py::test_hitl_partial_approve         PASSED
tests/test_agents.py::test_gdp_violation_logged         PASSED
tests/test_agents.py::test_low_risk_auto_approved       PASSED
...
39 passed in 4.21s
```

### Presenter says:
> "39 tests. Zero mocks on the core pipeline. The LLM is tested with a real fallback
> path so every test runs without an API key. The audit trail is tested for
> immutability. The HITL queue is tested for timeout, partial approval, and rejection.
> This is production-grade, not a prototype."

---

## Quick Reference Card

| Step | Terminal Command | Browser Action |
|------|-----------------|----------------|
| 1 | `python main.py test-pipeline` | — |
| 2 | `python main.py dashboard` | Open `http://localhost:8080` |
| 3 | `python main.py simulate --shipments 3 --ticks 30` | Watch cards appear |
| 4 | Watch for Tick 5 CRITICAL | See red-bordered card |
| 5 | — | Uncheck ESCALATE_CUSTOMS → type name → Approve |
| 6 | — | Filter audit log → HITL_DECISION |
| 7 | Scroll Terminal 2 for cascade output | — |
| 8 | Show pytest output (pre-run) | — |

## Fallback if simulation doesn't produce CRITICAL

If the random scenario assignment doesn't give a CRITICAL shipment in time:

```
python main.py test-pipeline
```

The smoke-test payload is **hardcoded** Mumbai → California with DELAYED status,
12.8°C temperature, and 80% humidity — it will **always** produce HIGH risk
with 3 anomalies. Use it as the approval demo instead of the live simulation card.
The HITL queue, audit log, and cascade output all behave identically.
