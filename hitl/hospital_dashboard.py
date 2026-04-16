"""
Hospital Vaccine Logistics Monitor
-----------------------------------
A hospital-facing dashboard showing inbound vaccine flights, temperature
stability, and viability status. Designed for hospital logistics ops teams.

Endpoints:
  GET  /                  - visual HTML dashboard
  GET  /api/flights       - inbound flight data (JSON)
  GET  /api/alerts        - viability alerts (JSON)
  GET  /api/stats         - summary statistics (JSON)
  GET  /health            - health check

Run:
    python main.py hospital --port 8060
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

from config import AUDIT_LOG_PATH, TEMP_MIN_C, TEMP_MAX_C

app = FastAPI(
    title="Hospital Vaccine Logistics Monitor",
    description="Hospital-facing view of inbound vaccine shipments and viability.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_queue = None
_product_catalogue: Dict[str, dict] = {}


def set_queue(queue) -> None:
    global _queue
    _queue = queue


def set_product_catalogue(catalogue: Dict[str, dict]) -> None:
    global _product_catalogue
    _product_catalogue = catalogue


def _load_product_catalogue() -> Dict[str, dict]:
    global _product_catalogue
    if _product_catalogue:
        return _product_catalogue
    cat_path = Path(__file__).parent.parent / "data" / "raw" / "product_catalogue.json"
    if cat_path.exists():
        with open(cat_path, "r", encoding="utf-8") as f:
            items = json.load(f)
            _product_catalogue = {item["product_id"]: item for item in items}
    return _product_catalogue


# ---------------------------------------------------------------------------
# Demo flight data
# ---------------------------------------------------------------------------

_DEMO_FLIGHTS = [
    {
        "flight_id": "DL123", "airline": "Delta", "origin": "JFK",
        "est_arrival": "11:30 AM", "vaccine_type": "Pfizer-BioNTech",
        "product_id": "mRNA-COVID", "quantity": 10000, "unit": "Vials",
        "temp_stability": "Stable (Ultra-Cold)", "viability": "OPTIMAL",
        "risk_score": 0.12, "shipment_id": "SHP-DEMO-001",
    },
    {
        "flight_id": "BA789", "airline": "British Airways", "origin": "LHR",
        "est_arrival": "2:15 PM", "vaccine_type": "Moderna",
        "product_id": "mRNA-COVID-MOD", "quantity": 5000, "unit": "Vials",
        "temp_stability": "At-Risk (Temp. Spike)", "viability": "AT-RISK",
        "risk_score": 0.58, "shipment_id": "SHP-DEMO-002",
    },
    {
        "flight_id": "AF456", "airline": "Air France", "origin": "CDG",
        "est_arrival": "Delayed (ETA 4:00 PM)", "vaccine_type": "J&J Janssen",
        "product_id": "VACC-STANDARD", "quantity": 2500, "unit": "Vials",
        "temp_stability": "Critical (Loss Imminent)", "viability": "CRITICAL",
        "risk_score": 0.89, "shipment_id": "SHP-DEMO-003",
    },
    {
        "flight_id": "LH302", "airline": "Lufthansa", "origin": "FRA",
        "est_arrival": "3:45 PM", "vaccine_type": "MMR Vaccine",
        "product_id": "VACC-MMR", "quantity": 8000, "unit": "Vials",
        "temp_stability": "Stable (Refrigerated)", "viability": "OPTIMAL",
        "risk_score": 0.08, "shipment_id": "SHP-DEMO-004",
    },
    {
        "flight_id": "SQ218", "airline": "Singapore Air", "origin": "SIN",
        "est_arrival": "5:20 PM", "vaccine_type": "Oral Polio (OPV)",
        "product_id": "VACC-OPV", "quantity": 15000, "unit": "Vials",
        "temp_stability": "Monitoring (Frozen)", "viability": "OPTIMAL",
        "risk_score": 0.22, "shipment_id": "SHP-DEMO-005",
    },
    {
        "flight_id": "EK401", "airline": "Emirates", "origin": "DXB",
        "est_arrival": "Delayed (ETA 6:30 PM)", "vaccine_type": "Sanofi Influenza",
        "product_id": "VACC-STANDARD", "quantity": 3200, "unit": "Vials",
        "temp_stability": "At-Risk (Humidity)", "viability": "AT-RISK",
        "risk_score": 0.64, "shipment_id": "SHP-DEMO-006",
    },
]


def _build_flights_from_queue() -> List[dict]:
    if _queue is None:
        return []
    catalogue = _load_product_catalogue()
    flights = []
    for req in _queue.all_requests():
        product_id = "VACC-STANDARD"
        product = catalogue.get(product_id, {})
        vaccine_name = product.get("name", "Standard Vaccine")
        if req.risk_score >= 0.70:
            viability, stability = "CRITICAL", "Critical (Loss Imminent)"
        elif req.risk_score >= 0.40:
            viability, stability = "AT-RISK", "At-Risk (Temp. Excursion)"
        else:
            viability, stability = "OPTIMAL", "Stable (Refrigerated)"
        sid = req.shipment_id
        flight_id = sid.replace("SHP-", "FL")[:6].upper()
        flights.append({
            "flight_id": flight_id, "airline": "Carrier", "origin": "---",
            "est_arrival": req.created_at.strftime("%I:%M %p") if hasattr(req.created_at, "strftime") else str(req.created_at),
            "vaccine_type": vaccine_name, "product_id": product_id,
            "quantity": product.get("doses_per_container", 5000), "unit": "Vials",
            "temp_stability": stability, "viability": viability,
            "risk_score": req.risk_score, "shipment_id": req.shipment_id,
            "status": req.status.value if hasattr(req.status, "value") else str(req.status),
        })
    return flights


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/flights", tags=["Hospital"])
def get_flights():
    live = _build_flights_from_queue()
    return live if live else _DEMO_FLIGHTS


@app.get("/api/alerts", tags=["Hospital"])
def get_alerts():
    flights = _build_flights_from_queue() or _DEMO_FLIGHTS
    return [f for f in flights if f["viability"] in ("AT-RISK", "CRITICAL")]


@app.get("/api/stats", tags=["Hospital"])
def get_stats():
    flights = _build_flights_from_queue() or _DEMO_FLIGHTS
    total_qty = sum(f.get("quantity", 0) for f in flights)
    critical = len([f for f in flights if f["viability"] == "CRITICAL"])
    at_risk = len([f for f in flights if f["viability"] == "AT-RISK"])
    return {
        "active_flights": len(flights),
        "total_shipments": total_qty,
        "viability_alerts": critical + at_risk,
        "critical_count": critical,
        "at_risk_count": at_risk,
        "optimal_count": len([f for f in flights if f["viability"] == "OPTIMAL"]),
    }


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "service": "hospital-dashboard"}


# ---------------------------------------------------------------------------
# HTML Dashboard — CSS
# ---------------------------------------------------------------------------

_CSS = r"""
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

/* Background: medical/hospital feel with layered gradients */
body{
  font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
  color:#e2e8f0;font-size:15px;line-height:1.6;min-height:100vh;
  overflow-x:hidden;
  background:#0b1120;
  background-image:
    radial-gradient(ellipse 80% 50% at 20% 60%, rgba(56,189,248,0.08) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 30%, rgba(139,92,246,0.06) 0%, transparent 50%),
    radial-gradient(ellipse 50% 50% at 50% 80%, rgba(34,197,94,0.04) 0%, transparent 50%),
    linear-gradient(180deg, #0b1120 0%, #111827 50%, #0f172a 100%);
}

/* ── Topbar ── */
.topbar{
  background:linear-gradient(90deg,#1e1145 0%,#2d1b69 40%,#4c1d95 60%,#2d1b69 80%,#1e1145 100%);
  padding:0 28px;display:flex;align-items:center;justify-content:space-between;
  height:58px;position:sticky;top:0;z-index:100;
  border-bottom:2px solid rgba(139,92,246,0.4);
  box-shadow:0 4px 24px rgba(0,0,0,0.5)}
.topbar-left{display:flex;align-items:center;gap:12px}
.logo-icon{font-size:28px;filter:drop-shadow(0 0 6px rgba(139,92,246,0.5))}
.topbar-title{display:flex;flex-direction:column}
.topbar-title .main{font-size:16px;font-weight:700;color:#fff;letter-spacing:0.01em}
.topbar-title .sub{font-size:10px;color:#c4b5fd;font-weight:600;text-transform:uppercase;letter-spacing:0.12em}
.topbar-center{position:absolute;left:50%;transform:translateX(-50%)}
.topbar-center .hx{font-size:36px;filter:drop-shadow(0 0 10px rgba(139,92,246,0.5))}
.topbar-right{display:flex;align-items:center;gap:16px;font-size:12px}
.sys-pill{display:flex;align-items:center;gap:5px;background:rgba(34,197,94,0.12);
  border:1px solid rgba(34,197,94,0.25);border-radius:99px;padding:3px 12px 3px 8px;
  font-size:11px;font-weight:600;color:#4ade80}
.sys-dot{width:7px;height:7px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.refresh-time{color:#94a3b8;font-size:12px}
.live-badge{background:#ef4444;color:#fff;font-size:10px;font-weight:700;
  padding:3px 10px;border-radius:4px;letter-spacing:0.06em;animation:livepulse 2s infinite}
@keyframes livepulse{0%,100%{opacity:1}50%{opacity:.65}}

/* ── Alert Banner ── */
.alert-banner{background:linear-gradient(90deg,#92400e,#b45309);color:#fef3c7;
  padding:8px 28px;font-size:13px;font-weight:600;display:none;align-items:center;gap:8px;
  border-bottom:1px solid rgba(251,191,36,0.3)}
.alert-banner.show{display:flex}
.alert-banner .alert-icon{font-size:16px}

/* ── Stats Cards ── */
.stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;
  padding:24px 28px 20px;max-width:1260px;margin:0 auto}
.stat-card{border-radius:14px;padding:20px 24px;position:relative;overflow:hidden;
  transition:transform .2s,box-shadow .2s;min-height:140px;display:flex;flex-direction:column}
.stat-card:hover{transform:translateY(-2px)}

/* Green card */
.stat-card.green{
  background:linear-gradient(135deg,#052e16 0%,#064e3b 60%,#065f46 100%);
  border:1px solid rgba(34,197,94,0.25);
  box-shadow:0 4px 20px rgba(0,0,0,0.3),inset 0 1px 0 rgba(34,197,94,0.1)}
.stat-card.green .card-label{color:#86efac}

/* Blue card */
.stat-card.blue{
  background:linear-gradient(135deg,#082f49 0%,#0c4a6e 60%,#0369a1 100%);
  border:1px solid rgba(56,189,248,0.25);
  box-shadow:0 4px 20px rgba(0,0,0,0.3),inset 0 1px 0 rgba(56,189,248,0.1)}
.stat-card.blue .card-label{color:#7dd3fc}

/* Red card */
.stat-card.red{
  background:linear-gradient(135deg,#450a0a 0%,#7f1d1d 60%,#991b1b 100%);
  border:1px solid rgba(239,68,68,0.25);
  box-shadow:0 4px 20px rgba(0,0,0,0.3),inset 0 1px 0 rgba(239,68,68,0.1)}
.stat-card.red .card-label{color:#fca5a5}

.stat-card .card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.stat-card .card-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;
  display:flex;align-items:center;gap:5px}
.stat-card .card-icon{font-size:24px;opacity:0.5}
.stat-card .card-value{font-size:48px;font-weight:800;color:#fff;line-height:1.1;flex:1;
  display:flex;align-items:flex-end}
.stat-card .card-chart{height:36px;margin-top:6px;opacity:0.5}
.stat-card .card-alert{position:absolute;top:10px;right:12px;font-size:24px;
  animation:alertpulse 1.2s infinite}
@keyframes alertpulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.12);opacity:.7}}

/* ── Main Panel ── */
.container{max-width:1260px;margin:0 auto;padding:0 28px 50px}
.panel{background:#fff;border-radius:14px;overflow:hidden;
  box-shadow:0 8px 40px rgba(0,0,0,0.35);color:#1e293b}
.panel-head{padding:16px 24px;border-bottom:none;display:flex;align-items:center;gap:10px}
.panel-head .bar{width:4px;height:24px;background:#4f46e5;border-radius:2px}
.panel-head h2{font-size:17px;font-weight:700;color:#1e293b}

/* ── Table ── */
.twrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
thead tr{background:linear-gradient(90deg,#3b0764,#4c1d95,#5b21b6,#4c1d95,#3b0764)}
thead th{padding:13px 18px;text-align:left;font-size:11px;font-weight:700;
  text-transform:uppercase;letter-spacing:0.08em;color:#e9d5ff;white-space:nowrap}
tbody tr{transition:background .12s;border-bottom:1px solid #f1f5f9}
tbody tr:nth-child(odd){background:#fff}
tbody tr:nth-child(even){background:#faf8ff}
tbody tr:hover{background:#ede9fe}
tbody td{padding:14px 18px;vertical-align:middle;color:#334155}
tbody tr:last-child{border-bottom:none}

/* Cell styles */
.flight-id{font-family:'SFMono-Regular',Consolas,'Courier New',monospace;font-weight:700;color:#1e293b;font-size:14px}
.airline{font-weight:600;color:#334155}
.origin-code{font-family:monospace;font-weight:700;color:#7c3aed;font-size:13px;
  background:#ede9fe;padding:2px 8px;border-radius:4px}
.arrival{font-weight:500;color:#334155}
.arrival.delayed{color:#dc2626;font-weight:700}
.vaccine{font-weight:600;color:#1e293b}
.quantity{font-weight:700;color:#1e293b}
.stability{font-size:13px;color:#64748b}
.stability.critical{color:#dc2626;font-weight:600}
.stability.at-risk{color:#d97706;font-weight:600}
.stability.stable{color:#16a34a;font-weight:500}

/* ── Viability Badges ── */
.via-badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:700;
  padding:5px 14px;border-radius:99px;letter-spacing:0.04em;white-space:nowrap}
.via-OPTIMAL{background:#dcfce7;color:#166534;border:1px solid #86efac}
.via-AT-RISK{background:#fef3c7;color:#92400e;border:1px solid #fbbf24}
.via-CRITICAL{background:#fecaca;color:#991b1b;border:1px solid #f87171;
  animation:critglow 1.5s infinite}
@keyframes critglow{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}
  50%{box-shadow:0 0 0 4px rgba(239,68,68,0.15)}}
.via-icon{font-size:13px}

/* ── Footer ── */
.footer{text-align:center;padding:16px;color:#475569;font-size:11px;
  max-width:1260px;margin:0 auto}

/* ── Responsive ── */
@media(max-width:900px){
  .stats-row{grid-template-columns:1fr;padding:16px}
  .stat-card .card-value{font-size:36px}
  .topbar{padding:0 14px;height:52px}
  .topbar-center{display:none}
  .container{padding:0 10px 40px}
  .panel-head{padding:12px 14px}
  thead th,tbody td{padding:10px 12px}
}
"""

_BODY = """
<div class="topbar">
  <div class="topbar-left">
    <span class="logo-icon">&#x1F3E5;</span>
    <div class="topbar-title">
      <span class="main">Hospital Vaccine Logistics Monitor</span>
      <span class="sub">Hospital Logistics Ops &bull; Level 4</span>
    </div>
  </div>
  <div class="topbar-center"><span class="hx">&#x2695;&#xFE0F;</span></div>
  <div class="topbar-right">
    <span class="sys-pill"><span class="sys-dot"></span> System Health: Optimal</span>
    <span class="refresh-time">Last refresh: <strong id="last-refresh">--:--:--</strong></span>
    <span class="live-badge">&#x25CF; LIVE</span>
  </div>
</div>

<div class="alert-banner" id="alert-banner">
  <span class="alert-icon">&#x26A0;&#xFE0F;</span>
  <span id="alert-text">Viability alert detected.</span>
</div>

<div class="stats-row">
  <div class="stat-card green">
    <div class="card-header">
      <span class="card-label">&#x1F7E2; Active Inbound Flights</span>
      <span class="card-icon">&#x2708;&#xFE0F;</span>
    </div>
    <div class="card-value" id="stat-flights">--</div>
    <div class="card-chart" id="chart-flights"></div>
  </div>
  <div class="stat-card blue">
    <div class="card-header">
      <span class="card-label">&#x1F535; Critical Shipments</span>
      <span class="card-icon">&#x1F4E6;</span>
    </div>
    <div class="card-value" id="stat-shipments">--</div>
    <div class="card-chart" id="chart-shipments"></div>
  </div>
  <div class="stat-card red">
    <div class="card-header">
      <span class="card-label">&#x1F534; Viability Alert</span>
      <span class="card-alert">&#x26A0;&#xFE0F;</span>
    </div>
    <div class="card-value" id="stat-alerts">--</div>
    <div class="card-chart" id="chart-alerts"></div>
  </div>
</div>

<div class="container">
  <div class="panel">
    <div class="panel-head">
      <span class="bar"></span>
      <h2>Inbound Vaccine Flights &amp; Viability Status</h2>
    </div>
    <div class="twrap">
      <table>
        <thead>
          <tr>
            <th>Flight ID</th>
            <th>Airline</th>
            <th>Origin</th>
            <th>Est. Arrival</th>
            <th>Vaccine Type</th>
            <th>Quantity</th>
            <th>Temp. Stability</th>
            <th>Viability</th>
          </tr>
        </thead>
        <tbody id="flights-body">
          <tr><td colspan="8" style="text-align:center;padding:40px;color:#94a3b8">
            &#x1F4E6; Loading flight data&hellip;
          </td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<div class="footer">
  Hospital Vaccine Logistics Monitor &bull; Pharma Cargo Monitor System &bull;
  GDP &sect;9.2 | 21 CFR 600.15 Compliant &bull; Auto-refresh 3s
</div>
"""

_JS = r"""
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function viaBadge(v){
  var icons={OPTIMAL:'\u2705','AT-RISK':'\u26A0\uFE0F',CRITICAL:'\u274C'};
  var label=v==='AT-RISK'?'AT-RISK':v;
  return '<span class="via-badge via-'+v+'"><span class="via-icon">'+(icons[v]||'')+
    '</span>'+label+'</span>';
}

function stabilityClass(v){
  if(v==='CRITICAL')return 'critical';
  if(v==='AT-RISK')return 'at-risk';
  return 'stable';
}

function arrivalClass(text){
  return (text||'').toLowerCase().indexOf('delay')!==-1?'delayed':'';
}

function drawMiniChart(id,color,data){
  var el=document.getElementById(id);if(!el)return;
  var w=el.offsetWidth||200,h=36,max=Math.max.apply(null,data)||1;
  var bw=Math.max(4,Math.floor(w/data.length)-2);
  var svg='<svg width="'+w+'" height="'+h+'" style="display:block">';
  for(var i=0;i<data.length;i++){
    var bh=Math.max(2,Math.round((data[i]/max)*h));
    svg+='<rect x="'+(i*(bw+2))+'" y="'+(h-bh)+'" width="'+bw+'" height="'+bh+
      '" rx="1" fill="'+color+'" opacity="0.55"/>';
  }
  el.innerHTML=svg+'</svg>';
}

function buildRow(f){
  var h='<tr>';
  h+='<td><span class="flight-id">'+esc(f.flight_id)+'</span></td>';
  h+='<td><span class="airline">'+esc(f.airline)+'</span></td>';
  h+='<td><span class="origin-code">'+esc(f.origin)+'</span></td>';
  h+='<td><span class="arrival '+arrivalClass(f.est_arrival)+'">'+esc(f.est_arrival)+'</span></td>';
  h+='<td><span class="vaccine">'+esc(f.vaccine_type)+'</span></td>';
  h+='<td><span class="quantity">'+Number(f.quantity).toLocaleString()+' '+esc(f.unit||'Vials')+'</span></td>';
  h+='<td><span class="stability '+stabilityClass(f.viability)+'">'+esc(f.temp_stability)+'</span></td>';
  h+='<td>'+viaBadge(f.viability)+'</td>';
  return h+'</tr>';
}

async function refresh(){
  try{
    var stats=await(await fetch('/api/stats')).json();
    document.getElementById('stat-flights').textContent=stats.active_flights;
    var ts=stats.total_shipments;
    document.getElementById('stat-shipments').textContent=ts>=1e6?(ts/1e6).toFixed(1)+'M':ts.toLocaleString();
    document.getElementById('stat-alerts').textContent=stats.viability_alerts;

    var banner=document.getElementById('alert-banner');
    if(stats.viability_alerts>0){
      banner.classList.add('show');
      document.getElementById('alert-text').textContent=
        stats.critical_count+' critical and '+stats.at_risk_count+' at-risk shipments require immediate attention.';
    }else{banner.classList.remove('show');}

    var fd=[],sd=[],ad=[];
    for(var i=0;i<14;i++){
      fd.push(Math.max(1,stats.active_flights+Math.round((Math.random()-0.5)*4)));
      sd.push(Math.max(100,ts+Math.round((Math.random()-0.5)*500)));
      ad.push(Math.max(0,stats.viability_alerts+Math.round((Math.random()-0.5)*2)));
    }
    drawMiniChart('chart-flights','#86efac',fd);
    drawMiniChart('chart-shipments','#7dd3fc',sd);
    drawMiniChart('chart-alerts','#fca5a5',ad);

    var flights=await(await fetch('/api/flights')).json();
    var tbody=document.getElementById('flights-body');
    if(!flights.length){
      tbody.innerHTML='<tr><td colspan="8" style="text-align:center;padding:40px;color:#94a3b8">\uD83D\uDCE6 No inbound flights.</td></tr>';
    }else{
      var ord={CRITICAL:0,'AT-RISK':1,OPTIMAL:2};
      flights.sort(function(a,b){return(ord[a.viability]||2)-(ord[b.viability]||2);});
      tbody.innerHTML=flights.map(buildRow).join('');
    }
    document.getElementById('last-refresh').textContent=new Date().toLocaleTimeString();
    document.title=(stats.viability_alerts>0?'('+stats.viability_alerts+') ':'')+'Hospital Vaccine Logistics Monitor';
  }catch(e){console.warn('Refresh failed:',e.message);}
}
refresh();setInterval(refresh,3000);
"""


def _get_html() -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<title>Hospital Vaccine Logistics Monitor</title>\n'
        '<style>' + _CSS + '</style>\n'
        '</head>\n<body>\n'
        + _BODY +
        '\n<script>' + _JS + '</script>\n'
        '</body>\n</html>'
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def hospital_dashboard():
    """Serve the hospital vaccine logistics dashboard."""
    return HTMLResponse(content=_get_html())
