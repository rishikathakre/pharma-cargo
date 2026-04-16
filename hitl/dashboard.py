"""
HITL Dashboard (FastAPI)
------------------------
REST API + visual HTML dashboard for human operators.

Endpoints:
  GET  /                        – visual HTML dashboard (operator screen)
  GET  /queue                   – pending approval requests (JSON)
  GET  /queue/all               – all requests any status (JSON)
  GET  /queue/{id}              – single request (JSON)
  POST /queue/{id}/approve      – approve (optionally partial)
  POST /queue/{id}/reject       – reject
  GET  /audit?n=50              – last N audit log records (JSON)
  GET  /health                  – health check

Run:
    python main.py dashboard
    # then open http://localhost:8080
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional
import threading

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from agents.risk_agent import RecommendedAction
from hitl.approval_queue import ApprovalQueue, ApprovalRequest
from config import AUDIT_LOG_PATH

app = FastAPI(
    title="Pharma Cargo Monitor - HITL Dashboard",
    description="Human-in-the-loop approval interface for AI-recommended actions.",
    version="1.0.0",
)

_queue: Optional[ApprovalQueue] = None
_orchestrator = None
_sim_lock = threading.Lock()

# Reroute plan results pushed here by the orchestrator after execution
_reroute_results: dict = {}          # shipment_id → reroute plan dict
_reroute_lock = threading.Lock()


def push_reroute_result(shipment_id: str, plan_dict: dict) -> None:
    """Called by the orchestrator after a reroute plan is executed."""
    with _reroute_lock:
        _reroute_results[shipment_id] = plan_dict


def set_queue(queue: ApprovalQueue) -> None:
    global _queue
    _queue = queue


def set_orchestrator(orchestrator) -> None:
    """Inject a CascadeOrchestrator instance that uses the same ApprovalQueue."""
    global _orchestrator
    _orchestrator = orchestrator


def _get_orchestrator():
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialised — call set_orchestrator() at startup.")
    return _orchestrator


def _get_queue() -> ApprovalQueue:
    if _queue is None:
        raise RuntimeError("ApprovalQueue not initialised — call set_queue() at startup.")
    return _queue


class ApproveRequest(BaseModel):
    operator:         str
    approved_actions: Optional[List[str]] = None
    notes:            str = ""


class RejectRequest(BaseModel):
    operator: str
    notes:    str = ""


class SimulateRequest(BaseModel):
    shipments: int = 3
    ticks: int = 20


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Pharma Cargo Monitor - HITL Dashboard</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: linear-gradient(135deg, #ede9fe 0%, #fce7f3 40%, #e0f2fe 100%);
  min-height: 100vh;
  font-size: 15px;
  color: #1e1b4b;
}

/* ── NAV ── */
.nav {
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 60%, #9333ea 100%);
  padding: 0 32px;
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 4px 20px rgba(79,70,229,0.4);
}
.nav-brand { display: flex; align-items: center; gap: 12px; }
.nav-icon {
  width: 38px; height: 38px; border-radius: 10px;
  background: rgba(255,255,255,0.18);
  display: flex; align-items: center; justify-content: center;
}
.nav-title { color: #fff; font-size: 17px; font-weight: 800; letter-spacing: -0.02em; }
.nav-sub   { color: #c4b5fd; font-size: 11px; font-weight: 500; margin-top: 1px; }
.nav-right { display: flex; align-items: center; gap: 16px; }
.nav-time  { color: #c4b5fd; font-size: 13px; }
.nav-time strong { color: #fff; }
.live-pill {
  display: flex; align-items: center; gap: 7px;
  background: rgba(255,255,255,0.15);
  border-radius: 99px; padding: 5px 14px;
}
.live-dot {
  width: 8px; height: 8px; border-radius: 50%; background: #4ade80;
  animation: blink 2s infinite;
}
.live-label { color: #fff; font-size: 12px; font-weight: 700; letter-spacing: 0.06em; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.4} }

/* ── STATS ── */
.stats-row {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 16px;
  max-width: 1200px;
  margin: 28px auto 0;
  padding: 0 24px;
}
.stat-card {
  background: rgba(255,255,255,0.75);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 18px;
  padding: 20px;
  box-shadow: 0 2px 12px rgba(124,58,237,0.08);
  transition: transform 0.2s, box-shadow 0.2s;
}
.stat-card:hover { transform: translateY(-3px); box-shadow: 0 8px 28px rgba(124,58,237,0.15); }
.stat-dot  { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.stat-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; display: flex; align-items: center; margin-bottom: 8px; }
.stat-value { font-size: 38px; font-weight: 800; line-height: 1; }
.c-amber  { color: #d97706; } .d-amber  { background: #fbbf24; }
.c-green  { color: #059669; } .d-green  { background: #34d399; }
.c-rose   { color: #e11d48; } .d-rose   { background: #fb7185; }
.c-violet { color: #7c3aed; } .d-violet { background: #a78bfa; }
.c-sky    { color: #0284c7; } .d-sky    { background: #38bdf8; }

/* ── MAIN LAYOUT ── */
.main { max-width: 1200px; margin: 24px auto 64px; padding: 0 24px; display: flex; flex-direction: column; gap: 22px; }

/* ── PANEL ── */
.panel {
  background: rgba(255,255,255,0.8);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,0.9);
  border-radius: 20px;
  box-shadow: 0 2px 16px rgba(124,58,237,0.07);
  overflow: hidden;
}
.panel-head {
  padding: 16px 24px;
  display: flex; align-items: center; gap: 12px;
  border-bottom: 1px solid rgba(237,233,254,0.8);
}
.panel-accent { width: 5px; height: 32px; border-radius: 99px; flex-shrink: 0; }
.pa-amber  { background: linear-gradient(to bottom, #fbbf24, #f97316); }
.pa-green  { background: linear-gradient(to bottom, #34d399, #059669); }
.pa-sky    { background: linear-gradient(to bottom, #38bdf8, #6d28d9); }
.panel-title { font-size: 15px; font-weight: 800; color: #1e1b4b; }
.panel-sub   { font-size: 11px; color: #a78bfa; margin-top: 1px; }
.panel-badge {
  margin-left: auto; font-size: 11px; font-weight: 800;
  padding: 3px 12px; border-radius: 99px;
}
.pb-amber  { background: #fef3c7; color: #92400e; }
.pb-green  { background: #d1fae5; color: #065f46; }
.pb-sky    { background: #dbeafe; color: #1e40af; }
.panel-body { padding: 20px 24px; }

/* ── EMPTY STATE ── */
.empty {
  text-align: center; padding: 44px 20px;
  color: #a78bfa; font-size: 14px; font-weight: 500;
  background: linear-gradient(135deg, #faf5ff, #f0f4ff);
  border-radius: 14px; border: 1.5px dashed #ddd6fe;
}

/* ── APPROVAL CARD ── */
.acard {
  border-radius: 14px; margin-bottom: 18px;
  background: #fff; overflow: hidden;
  box-shadow: 0 2px 12px rgba(124,58,237,0.07);
  border: 1px solid #e5e7eb;
  animation: fadein 0.3s ease;
}
@keyframes fadein { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
.acard:last-child { margin-bottom: 0; }
.acard-head {
  padding: 14px 20px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}
.acard-sid  { font-size: 16px; font-weight: 800; color: #1e1b4b; }
.chip {
  font-size: 12px; color: #6b7280; background: #f3f4f6;
  border: 1px solid #e5e7eb; border-radius: 6px; padding: 2px 9px;
}

/* ── BADGE ── */
.badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 99px; text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
.r-LOW      { background: #dcfce7; color: #15803d; }
.r-MEDIUM   { background: #fef9c3; color: #a16207; }
.r-HIGH     { background: #ffedd5; color: #c2410c; }
.r-CRITICAL { background: #fee2e2; color: #b91c1c; }
.s-APPROVED { background: #dcfce7; color: #15803d; }
.s-REJECTED { background: #fee2e2; color: #b91c1c; }
.s-PARTIAL  { background: #dbeafe; color: #1d4ed8; }
.s-TIMEOUT  { background: #ede9fe; color: #6d28d9; }
.s-PENDING  { background: #fef3c7; color: #92400e; }

/* ── CARD BODY ── */
.acard-body { padding: 18px 20px; }
.meta-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px,1fr)); gap: 12px; margin-bottom: 16px; }
.mlabel { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: #a78bfa; margin-bottom: 4px; }
.mval   { font-size: 14px; font-weight: 600; color: #1f2937; }
.mval.mono { font-family: 'Menlo', 'Consolas', monospace; font-size: 11px; color: #374151; }
.justif {
  background: linear-gradient(to right, #f5f3ff, #fdf4ff);
  border-left: 4px solid #a78bfa; border-radius: 0 12px 12px 0;
  padding: 13px 16px; font-size: 14px; color: #374151; line-height: 1.7;
  margin-bottom: 18px; font-style: italic;
}
.actions-label { font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; color: #a78bfa; margin-bottom: 10px; }
.apills { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
.apill {
  display: flex; align-items: center; gap: 7px;
  background: #f5f3ff; border: 1.5px solid #ddd6fe;
  border-radius: 8px; padding: 6px 12px; cursor: pointer;
  font-size: 13px; color: #4c1d95; transition: all 0.15s;
}
.apill:hover { background: #ede9fe; border-color: #a78bfa; }
.apill input[type=checkbox] { cursor: pointer; accent-color: #7c3aed; }
.decision-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding-top: 14px; border-top: 1px solid #f3e8ff; }

/* ── PENDING TABLE ── */
.ptable { width: 100%; border-collapse: collapse; font-size: 14px; }
.ptable thead tr { background: linear-gradient(135deg, #818cf8, #a78bfa); }
.ptable thead th { padding: 12px 16px; text-align: left; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; color: #fff; white-space: nowrap; }
.ptable tbody tr:nth-child(odd)  { background: #faf5ff; }
.ptable tbody tr:nth-child(even) { background: #fff; }
.ptable tbody tr:hover { background: #ede9fe; }
.ptable tbody td { padding: 11px 16px; border-bottom: 1px solid #f0e6ff; vertical-align: top; color: #374151; }
.ptable tbody tr:last-child td { border-bottom: none; }
.pactions { display: flex; flex-wrap: wrap; gap: 8px; }
.pactions label { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 8px; background: #f5f3ff; border: 1.5px solid #ddd6fe; color: #4c1d95; font-size: 12px; }
.pcontrols { display: grid; grid-template-columns: 1fr; gap: 8px; min-width: 260px; }
.pcontrols .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.pcontrols input[type=text] { width: 100%; }
.pcontrols .row .inp-op { width: 140px; }
.pcontrols .row .inp-nt { flex: 1; min-width: 160px; }

/* ── INPUTS ── */
input[type=text], select {
  background: #fff; border: 1.5px solid #e0e7ff; border-radius: 9px;
  padding: 8px 13px; font-size: 13px; color: #374151; outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
  font-family: inherit;
}
input[type=text]:focus, select:focus {
  border-color: #818cf8; box-shadow: 0 0 0 3px rgba(129,140,248,0.18);
}
.inp-op  { width: 165px; }
.inp-nt  { flex: 1; min-width: 200px; }

/* ── BUTTONS ── */
.btn {
  padding: 9px 22px; border-radius: 10px; border: none;
  font-size: 13px; font-weight: 700; cursor: pointer;
  transition: all 0.15s; white-space: nowrap; font-family: inherit;
}
.btn-approve {
  background: linear-gradient(135deg, #86efac, #4ade80);
  color: #14532d;
  box-shadow: 0 2px 10px rgba(74,222,128,0.35);
}
.btn-approve:hover { background: linear-gradient(135deg, #4ade80, #22c55e); transform: translateY(-1px); box-shadow: 0 4px 16px rgba(74,222,128,0.45); }
.btn-reject {
  background: linear-gradient(135deg, #fca5a5, #f87171);
  color: #7f1d1d;
  box-shadow: 0 2px 10px rgba(252,165,165,0.35);
}
.btn-reject:hover { background: linear-gradient(135deg, #f87171, #ef4444); transform: translateY(-1px); box-shadow: 0 4px 16px rgba(252,165,165,0.45); }

/* ── RESOLVED TABLE ── */
.twrap { overflow-x: auto; border-radius: 14px; border: 1px solid #e0e7ff; }
.rtable { width: 100%; border-collapse: collapse; font-size: 14px; }
.rtable thead tr { background: linear-gradient(135deg, #818cf8, #a78bfa); }
.rtable thead th { padding: 12px 16px; text-align: left; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: #fff; white-space: nowrap; }
.rtable tbody tr:nth-child(odd)  { background: #faf5ff; }
.rtable tbody tr:nth-child(even) { background: #fff; }
.rtable tbody tr:hover { background: #ede9fe; }
.rtable tbody td { padding: 11px 16px; border-bottom: 1px solid #f0e6ff; vertical-align: middle; color: #374151; }
.rtable tbody tr:last-child td { border-bottom: none; }

/* ── AUDIT TABLE ── */
.audit-ctrl { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
.audit-scroll { max-height: 340px; overflow-y: auto; border-radius: 14px; border: 1px solid #e0e7ff; }
.atable { width: 100%; border-collapse: collapse; font-size: 13px; }
.atable thead tr { background: linear-gradient(135deg, #818cf8, #a78bfa); }
.atable thead th { position: sticky; top: 0; z-index: 5; padding: 11px 14px; text-align: left; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: #fff; white-space: nowrap; }
.atable tbody tr:nth-child(odd)  { background: #faf5ff; }
.atable tbody tr:nth-child(even) { background: #fff; }
.atable tbody tr:hover { background: #ede9fe; }
.atable tbody td { padding: 9px 14px; border-bottom: 1px solid #f3e8ff; vertical-align: top; color: #4b5563; }
.atable tbody tr:last-child td { border-bottom: none; }
.mono { font-family: 'Menlo', 'Consolas', monospace; }

/* ── REROUTE INTEL CARD ── */
.rcard {
  border-radius: 16px; margin-bottom: 18px;
  background: #fff; overflow: hidden;
  box-shadow: 0 2px 16px rgba(124,58,237,0.1);
  border: 1.5px solid #e0e7ff;
  animation: fadein 0.4s ease;
}
.rcard:last-child { margin-bottom: 0; }
.rcard-head {
  padding: 14px 20px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  background: linear-gradient(135deg, #1e1b4b 0%, #312e81 60%, #4338ca 100%);
}
.rcard-sid  { font-size: 15px; font-weight: 800; color: #fff; }
.viable-yes { background: #bbf7d0; color: #14532d; padding: 3px 12px; border-radius: 99px; font-size: 11px; font-weight: 800; text-transform: uppercase; }
.viable-no  { background: #fecaca; color: #7f1d1d; padding: 3px 12px; border-radius: 99px; font-size: 11px; font-weight: 800; text-transform: uppercase; }
.rcard-body { padding: 18px 20px; }
.rcard-airport {
  display: flex; align-items: center; gap: 14px;
  background: linear-gradient(to right, #f0f9ff, #eff6ff);
  border: 1.5px solid #bae6fd; border-radius: 12px;
  padding: 14px 18px; margin-bottom: 16px;
}
.airport-iata { font-size: 36px; font-weight: 900; color: #1d4ed8; letter-spacing: -1px; font-family: 'Menlo','Consolas',monospace; }
.airport-info { flex: 1; }
.airport-name { font-size: 15px; font-weight: 700; color: #1e1b4b; }
.airport-city { font-size: 12px; color: #6b7280; margin-top: 2px; }
.airport-dist { font-size: 12px; color: #7c3aed; font-weight: 600; margin-top: 4px; }
.cold-chain-FULL    { display:inline-block; background:#d1fae5; color:#065f46; padding:2px 9px; border-radius:99px; font-size:10px; font-weight:700; text-transform:uppercase; margin-top:4px; }
.cold-chain-PARTIAL { display:inline-block; background:#fef3c7; color:#92400e; padding:2px 9px; border-radius:99px; font-size:10px; font-weight:700; text-transform:uppercase; margin-top:4px; }
.rmetrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px,1fr)); gap: 12px; margin-bottom: 16px; }
.rm-negative .mval { color: #dc2626; }
.rm-positive .mval { color: #059669; }
.rrationale {
  background: linear-gradient(to right, #f0f4ff, #faf5ff);
  border-left: 4px solid #6366f1; border-radius: 0 12px 12px 0;
  padding: 13px 16px; font-size: 14px; color: #374151; line-height: 1.7;
  margin-bottom: 12px;
}
.rreg {
  background: #fafafa; border: 1px solid #e5e7eb; border-radius: 10px;
  padding: 10px 14px; font-size: 12px; color: #6b7280; line-height: 1.6;
}

/* ── CONTEXT BANNER (in HITL card) ── */
.ctx-banner {
  background: linear-gradient(to right, #eff6ff, #f5f3ff);
  border: 1.5px solid #c7d2fe; border-radius: 10px;
  padding: 12px 16px; margin-bottom: 16px;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(130px,1fr)); gap: 8px;
}
.ctx-item { }
.ctx-label { font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing: 0.08em; color: #818cf8; margin-bottom: 3px; }
.ctx-val   { font-size: 13px; font-weight: 700; color: #1e1b4b; }
.ctx-val.warn { color: #dc2626; }
.ctx-val.ok   { color: #059669; }
.anomaly-pills { display:flex; flex-wrap:wrap; gap:6px; margin-bottom: 14px; }
.anomaly-pill  { font-size:11px; font-weight:700; padding:3px 10px; border-radius:99px; white-space:nowrap; }
.sev-CRITICAL { background:#fee2e2; color:#991b1b; }
.sev-HIGH     { background:#ffedd5; color:#9a3412; }
.sev-MEDIUM   { background:#fef9c3; color:#854d0e; }
.sev-LOW      { background:#dcfce7; color:#166534; }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #f5f3ff; border-radius: 3px; }
::-webkit-scrollbar-thumb { background: #c4b5fd; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #818cf8; }

/* ── TOAST ── */
#toast {
  position: fixed; bottom: 24px; right: 24px; z-index: 9999;
  border-radius: 14px; padding: 13px 22px;
  font-size: 14px; font-weight: 600;
  pointer-events: none; opacity: 0; transform: translateY(12px);
  transition: all 0.3s ease;
  box-shadow: 0 8px 32px rgba(0,0,0,0.15);
}
#toast.show { opacity: 1; transform: translateY(0); }
#toast.success { background: linear-gradient(135deg, #bbf7d0, #86efac); color: #14532d; }
#toast.error   { background: linear-gradient(135deg, #fecaca, #fca5a5); color: #7f1d1d; }
</style>
</head>
<body>

<nav class="nav">
  <div class="nav-brand">
    <div class="nav-icon">
      <svg width="22" height="22" fill="none" viewBox="0 0 24 24">
        <path stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
          d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0
             3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946
             3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138
             3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806
             3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438
             3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z"/>
      </svg>
    </div>
    <div>
      <div class="nav-title">Pharma Cargo Monitor</div>
      <div class="nav-sub">HITL Operations Dashboard</div>
    </div>
  </div>
  <div class="nav-right">
    <span class="nav-time">Last refresh: <strong id="last-refresh">&#8212;</strong></span>
    <div class="live-pill">
      <div class="live-dot"></div>
      <span class="live-label">LIVE</span>
    </div>
  </div>
</nav>

<div class="stats-row">
  <div class="stat-card">
    <div class="stat-label c-amber"><span class="stat-dot d-amber"></span>Pending</div>
    <div class="stat-value c-amber" id="stat-pending">&#8212;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label c-green"><span class="stat-dot d-green"></span>Approved</div>
    <div class="stat-value c-green" id="stat-approved">&#8212;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label c-rose"><span class="stat-dot d-rose"></span>Rejected</div>
    <div class="stat-value c-rose" id="stat-rejected">&#8212;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label c-violet"><span class="stat-dot d-violet"></span>Timed Out</div>
    <div class="stat-value c-violet" id="stat-timeout">&#8212;</div>
  </div>
  <div class="stat-card">
    <div class="stat-label c-sky"><span class="stat-dot d-sky"></span>Audit Events</div>
    <div class="stat-value c-sky" id="stat-audit">&#8212;</div>
  </div>
</div>

<div class="main">

  <div class="panel">
    <div class="panel-head">
      <div class="panel-accent pa-amber"></div>
      <div><div class="panel-title">Pending Approvals</div></div>
      <span class="panel-badge pb-amber" id="badge-pending">0</span>
    </div>
    <div class="panel-body">
      <div class="twrap" id="pending-wrap">
        <table class="ptable">
          <thead><tr>
            <th>Shipment</th>
            <th>Risk</th>
            <th>Score</th>
            <th>Justification</th>
            <th>Proposed_Actions</th>
            <th>Decision</th>
          </tr></thead>
          <tbody id="pending-body">
            <tr><td colspan="6" style="text-align:center;padding:28px;color:#a78bfa">Loading&#8230;</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-accent pa-green"></div>
      <div><div class="panel-title">Resolved Decisions</div></div>
      <span class="panel-badge pb-green" id="badge-resolved">0</span>
    </div>
    <div class="panel-body">
      <div id="resolved-wrap"><div class="empty">No resolved decisions yet.</div></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-accent" style="background:linear-gradient(to bottom,#6366f1,#1d4ed8)"></div>
      <div>
        <div class="panel-title">Reroute Intelligence</div>
        <div class="panel-sub">Gemini AI divert recommendations &amp; viability analysis</div>
      </div>
      <span class="panel-badge" style="background:#e0e7ff;color:#3730a3" id="badge-reroute">0</span>
    </div>
    <div class="panel-body">
      <div id="reroute-wrap"><div class="empty">No reroute decisions yet. Reroute plans appear here after a REROUTE_SHIPMENT action is approved and executed.</div></div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-accent pa-sky"></div>
      <div>
        <div class="panel-title">Audit Log</div>
        <div class="panel-sub">ALCOA+ &#8212; GDP &sect;8 / 21 CFR 211.68</div>
      </div>
      <span class="panel-badge pb-sky" id="badge-audit">0</span>
    </div>
    <div class="panel-body">
      <div class="audit-ctrl">
        <select id="audit-n">
          <option value="25">Last 25 records</option>
          <option value="50" selected>Last 50 records</option>
          <option value="100">Last 100 records</option>
          <option value="200">Last 200 records</option>
        </select>
        <select id="audit-type">
          <option value="">All event types</option>
          <option value="PIPELINE_RUN">PIPELINE_RUN</option>
          <option value="RISK_ASSESSMENT">RISK_ASSESSMENT</option>
          <option value="ANOMALY_DETECTED">ANOMALY_DETECTED</option>
          <option value="HITL_DECISION">HITL_DECISION</option>
          <option value="ACTION_RESULT">ACTION_RESULT</option>
          <option value="COMPLIANCE_VIOLATION">COMPLIANCE_VIOLATION</option>
        </select>
      </div>
      <div class="audit-scroll">
        <table class="atable">
          <thead><tr>
            <th>Timestamp (UTC)</th><th>Event Type</th>
            <th>Shipment ID</th><th>Payload</th>
          </tr></thead>
          <tbody id="audit-body">
            <tr><td colspan="4" style="text-align:center;padding:28px;color:#a78bfa">Loading&#8230;</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

</div>

<div id="toast"></div>

<script>
const RISK_LEVELS = ['LOW','MEDIUM','HIGH','CRITICAL'];
const RISK_S = {
  LOW:      {bg:'#dcfce7',color:'#15803d',border:'#86efac',hbg:'rgba(220,252,231,0.35)'},
  MEDIUM:   {bg:'#fef9c3',color:'#a16207',border:'#fde047',hbg:'rgba(254,249,195,0.35)'},
  HIGH:     {bg:'#ffedd5',color:'#c2410c',border:'#fdba74',hbg:'rgba(255,237,213,0.35)'},
  CRITICAL: {bg:'#fee2e2',color:'#b91c1c',border:'#fca5a5',hbg:'rgba(254,226,226,0.35)'},
};
const STATUS_S = {
  APPROVED:{bg:'#dcfce7',color:'#15803d'},
  REJECTED:{bg:'#fee2e2',color:'#b91c1c'},
  PARTIAL: {bg:'#dbeafe',color:'#1d4ed8'},
  TIMEOUT: {bg:'#ede9fe',color:'#6d28d9'},
  PENDING: {bg:'#fef3c7',color:'#92400e'},
};
function badge(bg,color,txt){
  return '<span style="background:'+bg+';color:'+color+';display:inline-block;font-size:11px;font-weight:700;padding:3px 10px;border-radius:99px;text-transform:uppercase;letter-spacing:0.04em;white-space:nowrap">'+txt+'</span>';
}
function rBadge(l){const s=RISK_S[l]||RISK_S.LOW;return badge(s.bg,s.color,l);}
function sBadge(s){const p=STATUS_S[s]||STATUS_S.PENDING;return badge(p.bg,p.color,s);}
function fmtTime(iso){if(!iso)return'&#8212;';return new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function fmtDate(iso){if(!iso)return'&#8212;';const d=new Date(iso);return d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function toast(msg,type){const el=document.getElementById('toast');el.textContent=msg;el.className='show '+(type||'success');setTimeout(()=>{el.className='';},3200);}

function buildContextBanner(req){
  const m=req.metadata||{};
  const hasReroute=(req.proposed_actions||[]).includes('REROUTE_SHIPMENT');
  if(!hasReroute && !Object.keys(m).length) return '';
  const batt=m.battery_pct!=null?m.battery_pct:null;
  const battClass=batt!=null?(batt<20?'warn':batt<50?'':'ok'):'';
  const battLabel=batt!=null?batt+'%':'—';
  const delay=m.delay_hours!=null?m.delay_hours.toFixed(1)+'h':'—';
  const wx=m.weather_severity!=null?(m.weather_severity*10).toFixed(1)+'/10':'—';
  const spoil=m.spoilage_prob!=null?(m.spoilage_prob*100).toFixed(1)+'%':'—';
  const anomalyPills=(m.anomalies||[]).map(a=>
    '<span class="anomaly-pill sev-'+a.severity+'">'+a.type.replace(/_/g,' ')+'</span>'
  ).join('');
  let ctx='<div class="ctx-banner">'+
    (m.destination?'<div class="ctx-item"><div class="ctx-label">Destination</div><div class="ctx-val">'+esc(m.destination)+'</div></div>':'') +
    (m.product_id?'<div class="ctx-item"><div class="ctx-label">Product</div><div class="ctx-val">'+esc(m.product_id)+'</div></div>':'') +
    (m.phase?'<div class="ctx-item"><div class="ctx-label">Phase</div><div class="ctx-val">'+esc(m.phase)+'</div></div>':'') +
    (batt!=null?'<div class="ctx-item"><div class="ctx-label">Battery</div><div class="ctx-val '+battClass+'">'+battLabel+'</div></div>':'') +
    '<div class="ctx-item"><div class="ctx-label">Delay</div><div class="ctx-val '+(m.delay_hours>2?'warn':'')+'">'+delay+'</div></div>'+
    '<div class="ctx-item"><div class="ctx-label">Weather</div><div class="ctx-val '+(m.weather_severity>0.6?'warn':'')+'">'+wx+'</div></div>'+
    '<div class="ctx-item"><div class="ctx-label">Spoilage Risk</div><div class="ctx-val '+(m.spoilage_prob>0.3?'warn':'ok')+'">'+spoil+'</div></div>'+
    (m.carrier?'<div class="ctx-item"><div class="ctx-label">Carrier</div><div class="ctx-val">'+esc(m.carrier)+'</div></div>':'') +
  '</div>';
  if(anomalyPills) ctx+='<div class="anomaly-pills">'+anomalyPills+'</div>';
  return ctx;
}

function buildCard(req){
  const rl=req.risk_level||'LOW', s=RISK_S[rl]||RISK_S.LOW;
  const cnt=(req.proposed_actions||[]).length;
  const pills=(req.proposed_actions||[]).map(a=>
    '<label class="apill"><input type="checkbox" name="act_'+req.request_id+'" value="'+a+'" checked/>'+a.replace(/_/g,' ')+'</label>'
  ).join('');
  return '<div class="acard" style="border-left:4px solid '+s.border+'">'+
    '<div class="acard-head" style="background:'+s.hbg+'">'+
      '<span class="acard-sid">'+req.shipment_id+'</span>'+
      rBadge(rl)+
      '<span class="chip">Score: '+req.risk_score.toFixed(4)+'</span>'+
      '<span class="chip">'+fmtTime(req.created_at)+'</span>'+
      '<span class="chip">'+cnt+' action'+(cnt!==1?'s':'')+'</span>'+
    '</div>'+
    '<div class="acard-body">'+
      buildContextBanner(req)+
      '<div class="meta-grid">'+
        '<div><div class="mlabel">Request ID</div><div class="mval mono" style="font-size:11px">'+req.request_id+'</div></div>'+
        '<div><div class="mlabel">Risk Level</div><div>'+rBadge(rl)+'</div></div>'+
        '<div><div class="mlabel">Risk Score</div><div class="mval">'+req.risk_score.toFixed(4)+'</div></div>'+
        '<div><div class="mlabel">Actions</div><div class="mval">'+cnt+'</div></div>'+
      '</div>'+
      '<div class="justif">'+(req.justification||'No justification provided.')+'</div>'+
      '<div class="actions-label">Select actions to approve</div>'+
      '<div class="apills">'+(pills||'<span style="color:#a78bfa;font-size:13px">No actions proposed</span>')+'</div>'+
      '<div class="decision-row">'+
        '<input class="inp-op" type="text" id="op-'+req.request_id+'" placeholder="Operator name"/>'+
        '<input class="inp-nt" type="text" id="nt-'+req.request_id+'" placeholder="Notes (optional)"/>'+
        '<button class="btn btn-approve" onclick="decide(\\''+req.request_id+'\\',\\'approve\\')">&#10003; Approve</button>'+
        '<button class="btn btn-reject"  onclick="decide(\\''+req.request_id+'\\',\\'reject\\')">&#10007; Reject</button>'+
      '</div>'+
    '</div>'+
  '</div>';
}

function buildReroute(plans){
  if(!plans.length) return '<div class="empty">No reroute decisions yet. Reroute plans appear here after a REROUTE_SHIPMENT action is approved and executed.</div>';
  return plans.map(p=>{
    const viable=p.viable;
    const viableBadge=viable
      ?'<span class="viable-yes">&#10003; Viable</span>'
      :'<span class="viable-no">&#10007; Not Viable &#8212; cold chain likely broken</span>';
    const iata=p.divert_airport_iata||'—';
    const aname=p.divert_airport_name||'Unknown Airport';
    const acity=p.divert_airport_city||'';
    const adist=p.divert_distance_km?p.divert_distance_km.toFixed(0)+' km away':'';
    const coldChain=p.divert_airport_iata?'<span class="cold-chain-FULL">Full Cold-Chain</span>':'';
    const eta=p.eta_hours!=null?p.eta_hours.toFixed(1)+'h':'—';
    const tts=p.time_to_spoilage_hours!=null?p.time_to_spoilage_hours.toFixed(1)+'h':'—';
    const margin=p.margin_hours!=null?p.margin_hours.toFixed(1)+'h':'—';
    const marginNeg=p.margin_hours!=null&&p.margin_hours<0;
    const batt=p.battery_pct!=null?p.battery_pct+'%':'—';
    const delay=p.delay_hours!=null?p.delay_hours.toFixed(1)+'h':'—';
    return '<div class="rcard">'+
      '<div class="rcard-head">'+
        '<span class="rcard-sid">&#9992; '+esc(p.shipment_id||'Unknown')+'</span>'+
        (p.risk_level?rBadge(p.risk_level):'')+
        viableBadge+
        '<span style="color:#c7d2fe;font-size:12px;margin-left:auto">'+fmtTime(p.assessed_at)+'</span>'+
      '</div>'+
      '<div class="rcard-body">'+
        (iata!=='—'?
        '<div class="rcard-airport">'+
          '<div class="airport-iata">'+esc(iata)+'</div>'+
          '<div class="airport-info">'+
            '<div class="airport-name">'+esc(aname)+'</div>'+
            '<div class="airport-city">'+esc(acity)+'</div>'+
            (adist?'<div class="airport-dist">'+adist+'</div>':'')+
            coldChain+
          '</div>'+
          '<div style="text-align:right">'+
            '<div class="mlabel">Recommended Divert</div>'+
            '<div style="font-size:12px;color:#6b7280;margin-top:4px">Path: '+esc(p.chosen_path||'—')+'</div>'+
          '</div>'+
        '</div>':'')+
        '<div class="rmetrics">'+
          '<div><div class="mlabel">ETA to Divert</div><div class="mval">'+eta+'</div></div>'+
          '<div><div class="mlabel">Time to Spoilage</div><div class="mval '+(p.time_to_spoilage_hours!=null&&p.time_to_spoilage_hours<1?'rm-negative':'')+'">'+tts+'</div></div>'+
          '<div class="'+(marginNeg?'rm-negative':'rm-positive')+'"><div class="mlabel">Margin</div><div class="mval">'+margin+'</div></div>'+
          '<div><div class="mlabel">Spoilage Risk</div><div class="mval '+(p.spoilage_prob>0.5?'rm-negative':'')+'">'+(p.spoilage_prob!=null?(p.spoilage_prob*100).toFixed(1)+'%':'—')+'</div></div>'+
          '<div><div class="mlabel">Battery</div><div class="mval '+(p.battery_pct!=null&&p.battery_pct<30?'rm-negative':'')+'">'+batt+'</div></div>'+
          '<div><div class="mlabel">Delay Accumulated</div><div class="mval">'+delay+'</div></div>'+
        '</div>'+
        (p.rationale?'<div class="rrationale"><strong style="color:#4338ca">Gemini Analysis:</strong> '+esc(p.rationale)+'</div>':'')+
        (p.regulatory_note?'<div class="rreg"><strong>Regulatory:</strong> '+esc(p.regulatory_note)+'</div>':'')+
      '</div>'+
    '</div>';
  }).join('');
}

function buildPendingTable(reqs){
  if(!reqs.length){
    document.getElementById('pending-body').innerHTML =
      '<tr><td colspan="6"><div class="empty">&#10003; No pending approvals &#8212; all shipments are clear.</div></td></tr>';
    return;
  }
  document.getElementById('pending-body').innerHTML = reqs.map(req=>{
    const rl=req.risk_level||'LOW';
    const just = esc((req.justification||'').slice(0, 220)) + ((req.justification||'').length>220?'&#8230;':'');
    const acts=(req.proposed_actions||[]);
    const pills = acts.length ? acts.map(a=>
      '<label><input type="checkbox" name="act_'+req.request_id+'" value="'+a+'" checked/> '+esc(a.replace(/_/g,' '))+'</label>'
    ).join('') : '<span style="color:#a78bfa">None</span>';

    const controls =
      '<div class="pcontrols">'+
        '<div class="row">'+
          '<input class="inp-op" type="text" id="op-'+req.request_id+'" placeholder="Operator"/>'+
          '<input class="inp-nt" type="text" id="nt-'+req.request_id+'" placeholder="Notes"/>'+
        '</div>'+
        '<div class="row">'+
          '<button class="btn btn-approve" onclick="decide(\\''+req.request_id+'\\',\\'approve\\')">&#10003; Approve</button>'+
          '<button class="btn btn-reject"  onclick="decide(\\''+req.request_id+'\\',\\'reject\\')">&#10007; Reject</button>'+
        '</div>'+
      '</div>';

    return '<tr>'+
      '<td style="font-weight:800;color:#1e1b4b">'+esc(req.shipment_id||'—')+'<div class="mono" style="font-size:11px;color:#9ca3af;margin-top:4px">'+esc(req.request_id||'')+'</div></td>'+
      '<td>'+rBadge(rl)+'</td>'+
      '<td class="mono" style="font-size:12px">'+Number(req.risk_score||0).toFixed(4)+'</td>'+
      '<td style="max-width:360px;line-height:1.45">'+just+'</td>'+
      '<td><div class="pactions">'+pills+'</div></td>'+
      '<td>'+controls+'</td>'+
    '</tr>';
  }).join('');
}

function buildResolved(items){
  if(!items.length) return '<div class="empty">No resolved decisions yet.</div>';
  const rows=items.map(r=>{
    const acts=(r.approved_actions||[]).map(a=>a.replace(/_/g,' ')).join(', ')||'&#8212;';
    return '<tr>'+
      '<td style="font-weight:700;color:#1e1b4b">'+r.shipment_id+'</td>'+
      '<td>'+rBadge(r.risk_level)+'</td>'+
      '<td class="mono" style="font-size:12px">'+r.risk_score.toFixed(4)+'</td>'+
      '<td>'+sBadge(r.status)+'</td>'+
      '<td style="font-size:13px;color:#6b7280">'+acts+'</td>'+
      '<td style="font-size:13px;color:#6b7280">'+(r.decided_by||'auto')+'</td>'+
      '<td style="font-size:13px;color:#6b7280">'+fmtTime(r.decided_at)+'</td>'+
      '<td style="font-size:13px;color:#6b7280">'+(r.notes||'&#8212;')+'</td>'+
    '</tr>';
  }).join('');
  return '<div class="twrap"><table class="rtable">'+
    '<thead><tr><th>Shipment</th><th>Risk</th><th>Score</th><th>Status</th><th>Actions</th><th>Decided By</th><th>Time</th><th>Notes</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>';
}

function buildAudit(records){
  if(!records.length){
    document.getElementById('audit-body').innerHTML='<tr><td colspan="4" style="text-align:center;padding:32px;color:#a78bfa">No audit records yet.</td></tr>';
    return;
  }
  document.getElementById('audit-body').innerHTML=records.map(r=>{
    const p=JSON.stringify(r.payload||{},null,2);
    return '<tr>'+
      '<td class="mono" style="font-size:11px;color:#9ca3af;white-space:nowrap">'+fmtDate(r.timestamp)+'</td>'+
      '<td class="mono" style="font-size:12px;color:#7c3aed;font-weight:700">'+(r.event_type||'&#8212;')+'</td>'+
      '<td class="mono" style="font-size:12px;color:#059669;font-weight:600">'+(r.shipment_id||'&#8212;')+'</td>'+
      '<td><div class="mono" style="font-size:11px;color:#6b7280;max-width:380px;word-break:break-all;white-space:pre-wrap">'+esc(p.slice(0,300))+(p.length>300?'&#8230;':'')+'</div></td>'+
    '</tr>';
  }).join('');
}

async function decide(id,action){
  const op=(document.getElementById('op-'+id)?.value||'').trim()||'operator';
  const nt=(document.getElementById('nt-'+id)?.value||'').trim();
  const body={operator:op,notes:nt};
  if(action==='approve') body.approved_actions=[...document.querySelectorAll('input[name="act_'+id+'"]:checked')].map(e=>e.value);
  try{
    const r=await fetch('/queue/'+id+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok) throw new Error((await r.json()).detail||r.statusText);
    toast(action==='approve'?'&#10003; Approved!':'&#10007; Rejected',action==='approve'?'success':'error');
    await refresh();
  }catch(e){toast('Error: '+e.message,'error');}
}

async function fetchAudit(){
  const n=document.getElementById('audit-n').value;
  const t=document.getElementById('audit-type').value;
  let url='/audit?n='+n; if(t) url+='&event_type='+encodeURIComponent(t);
  try{
    const d=await(await fetch(url)).json();
    document.getElementById('stat-audit').textContent=d.length;
    document.getElementById('badge-audit').textContent=d.length;
    buildAudit(d);
  }catch(e){
    document.getElementById('audit-body').innerHTML='<tr><td colspan="4" style="color:#e11d48;padding:14px">Audit fetch failed: '+e.message+'</td></tr>';
  }
}

async function refresh(){
  try{
    const all=await(await fetch('/queue/all')).json();
    const pend=all.filter(r=>r.status==='PENDING').sort((a,b)=>RISK_LEVELS.indexOf(b.risk_level)-RISK_LEVELS.indexOf(a.risk_level));
    const res=all.filter(r=>r.status!=='PENDING').sort((a,b)=>(b.decided_at||b.created_at).localeCompare(a.decided_at||a.created_at));
    document.getElementById('stat-pending').textContent=pend.length;
    document.getElementById('stat-approved').textContent=all.filter(r=>r.status==='APPROVED'||r.status==='PARTIAL').length;
    document.getElementById('stat-rejected').textContent=all.filter(r=>r.status==='REJECTED').length;
    document.getElementById('stat-timeout').textContent=all.filter(r=>r.status==='TIMEOUT').length;
    document.getElementById('badge-pending').textContent=pend.length;
    document.getElementById('badge-resolved').textContent=res.length;
    buildPendingTable(pend);
    document.getElementById('resolved-wrap').innerHTML=buildResolved(res);
    document.getElementById('last-refresh').textContent=new Date().toLocaleTimeString();
  }catch(e){console.warn('Refresh error:',e.message);}

  // Fetch reroute results from the map sim process (port 8090)
  try{
    const rp=await(await fetch('http://localhost:8090/api/reroute')).json();
    document.getElementById('badge-reroute').textContent=rp.length;
    document.getElementById('reroute-wrap').innerHTML=buildReroute(rp);
  }catch(e){console.warn('Reroute fetch error:',e.message);}

  await fetchAudit();
}

document.getElementById('audit-n').addEventListener('change',fetchAudit);
document.getElementById('audit-type').addEventListener('change',fetchAudit);
refresh();
setInterval(refresh,3000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(content=_HTML)


@app.get("/queue", response_model=List[dict], tags=["HITL"])
def list_pending():
    return [r.to_dict() for r in _get_queue().pending()]


@app.get("/queue/all", response_model=List[dict], tags=["HITL"])
def list_all():
    return [r.to_dict() for r in _get_queue().all_requests()]


@app.get("/queue/{request_id}", response_model=dict, tags=["HITL"])
def get_request(request_id: str):
    req = _get_queue().get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return req.to_dict()


@app.post("/queue/{request_id}/approve", response_model=dict, tags=["HITL"])
def approve_request(request_id: str, body: ApproveRequest):
    queue = _get_queue()
    req   = queue.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    actions = None
    if body.approved_actions is not None:
        try:
            actions = [RecommendedAction(a) for a in body.approved_actions]
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    updated = queue.approve(request_id, body.operator, actions, body.notes)

    # Forward approval to map sim so it can execute actions + apply reroute
    _forward_to_map_sim(request_id, "approve", body.operator, body.notes,
                        body.approved_actions)

    return updated.to_dict()


@app.post("/queue/{request_id}/reject", response_model=dict, tags=["HITL"])
def reject_request(request_id: str, body: RejectRequest):
    queue = _get_queue()
    req   = queue.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    updated = queue.reject(request_id, body.operator, body.notes)

    # Forward rejection to map sim
    _forward_to_map_sim(request_id, "reject", body.operator, body.notes)

    return updated.to_dict()


def _forward_to_map_sim(request_id: str, action: str, operator: str,
                        notes: str = "", approved_actions=None):
    """Forward HITL decision to the map sim so it can execute actions + reroute."""
    import urllib.request
    import json as _json
    import logging as _fwd_log

    _logger = _fwd_log.getLogger("hitl.forward")

    # Map sim runs on port 8090 by default (from start.py --map-port)
    map_port = 8090
    url = f"http://localhost:{map_port}/api/hitl/{request_id}/{action}"
    payload = {"operator": operator, "notes": f"[via HITL dashboard] {notes}"}
    if action == "approve" and approved_actions is not None:
        payload["approved_actions"] = approved_actions

    try:
        data = _json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            _logger.info("Forwarded %s to map sim: %s → %s", action, request_id[:12], resp.status)
    except Exception as exc:
        _logger.warning("Forward to map sim failed (port %d): %s", map_port, exc)


@app.get("/audit", response_model=List[dict], tags=["Compliance"])
def get_audit_log(
    n: int = Query(default=50, ge=1, le=500),
    event_type: Optional[str] = Query(default=None),
):
    path = Path(AUDIT_LOG_PATH)
    records = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if event_type is None or rec.get("event_type") == event_type:
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
    return list(reversed(records[-n:]))


@app.get("/reroute", response_model=List[dict], tags=["Reroute"])
def list_reroute_results():
    """Return all reroute plan results stored since last restart."""
    with _reroute_lock:
        return list(_reroute_results.values())


@app.get("/health", tags=["System"])
def health():
    q = _get_queue()
    return {"status": "ok", "pending_count": len(q.pending()), "total_count": len(q.all_requests())}


@app.post("/simulate", tags=["System"])
def simulate(body: SimulateRequest):
    """
    Run the simulation using the SAME in-memory queue as the dashboard.
    This makes pending approvals appear in the operator UI without needing
    any external message bus.
    """
    orch = _get_orchestrator()

    def _run():
        from simulation.stream_simulator import StreamSimulator
        from mock_services import start_mock_services

        with _sim_lock:
            start_mock_services()
            sim = StreamSimulator(n_shipments=body.shipments, interval_sec=0)
            for payload in sim.stream(max_ticks=body.ticks, realtime=False):
                orch.run(payload)

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "shipments": body.shipments, "ticks": body.ticks}
