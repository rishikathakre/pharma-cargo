"""
Hospital Notification Center
-----------------------------
Receives and displays real-time alerts sent to the hospital when a
NOTIFY_HOSPITAL or REROUTE_SHIPMENT action is approved and executed by
the agent pipeline.

No demo data — every card shown here was produced by the live system.

Endpoints:
  GET  /                      - HTML notification center
  GET  /api/notifications     - all notifications (JSON, newest first)
  GET  /health                - health check

Run (via start.py):
    python start.py            # hospital dashboard on port 8060
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(
    title="Hospital Notification Center",
    description="Live alerts from the Pharma Cargo Monitor agent pipeline.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Shared in-memory notification store
# Both this module and hospital_notifier.py run in the same OS process,
# so hospital_notifier can import push_notification() directly.
# ---------------------------------------------------------------------------
_notifications: List[dict] = []
_notif_lock = threading.Lock()


def push_notification(payload: dict) -> None:
    """Called by HospitalNotifier._send() to store a notification in-process."""
    record = dict(payload)
    if "received_at" not in record:
        record["received_at"] = datetime.now(timezone.utc).isoformat()
    with _notif_lock:
        _notifications.append(record)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/notifications", tags=["Hospital"])
def get_notifications() -> List[dict]:
    with _notif_lock:
        items = list(_notifications)
    return sorted(items, key=lambda x: x.get("received_at", ""), reverse=True)


@app.get("/health", tags=["System"])
def health():
    with _notif_lock:
        count = len(_notifications)
    return {"status": "ok", "service": "hospital-notification-center", "notification_count": count}


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Hospital Notification Center</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
  color:#e2e8f0;font-size:15px;line-height:1.6;min-height:100vh;
  overflow-x:hidden;
  background:#0b1120;
  background-image:
    radial-gradient(ellipse 80% 50% at 20% 60%,rgba(56,189,248,0.08) 0%,transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 30%,rgba(139,92,246,0.06) 0%,transparent 50%),
    linear-gradient(180deg,#0b1120 0%,#111827 50%,#0f172a 100%);
}

/* Topbar */
.topbar{
  background:linear-gradient(90deg,#1e1145 0%,#2d1b69 40%,#4c1d95 60%,#2d1b69 80%,#1e1145 100%);
  padding:0 28px;display:flex;align-items:center;justify-content:space-between;
  height:58px;position:sticky;top:0;z-index:100;
  border-bottom:2px solid rgba(139,92,246,0.4);
  box-shadow:0 4px 24px rgba(0,0,0,0.5)
}
.topbar-left{display:flex;align-items:center;gap:12px}
.logo-icon{font-size:26px}
.topbar-title .main{font-size:16px;font-weight:700;color:#fff}
.topbar-title .sub{font-size:10px;color:#c4b5fd;font-weight:600;text-transform:uppercase;letter-spacing:.12em}
.topbar-right{display:flex;align-items:center;gap:16px;font-size:12px}
.sys-pill{display:flex;align-items:center;gap:5px;background:rgba(34,197,94,0.12);
  border:1px solid rgba(34,197,94,0.25);border-radius:99px;padding:3px 12px 3px 8px;
  font-size:11px;font-weight:600;color:#4ade80}
.sys-dot{width:7px;height:7px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-badge{background:#ef4444;color:#fff;font-size:10px;font-weight:700;
  padding:3px 10px;border-radius:4px;letter-spacing:.06em;animation:livepulse 2s infinite}
@keyframes livepulse{0%,100%{opacity:1}50%{opacity:.65}}
.refresh-time{color:#94a3b8;font-size:12px}

/* Stats */
.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;
  padding:24px 28px 20px;max-width:1260px;margin:0 auto}
.stat-card{border-radius:14px;padding:18px 22px;min-height:110px;display:flex;flex-direction:column;
  transition:transform .2s}
.stat-card:hover{transform:translateY(-2px)}
.stat-card.green{background:linear-gradient(135deg,#052e16,#064e3b,#065f46);
  border:1px solid rgba(34,197,94,0.25)}
.stat-card.amber{background:linear-gradient(135deg,#451a03,#78350f,#92400e);
  border:1px solid rgba(251,191,36,0.25)}
.stat-card.red{background:linear-gradient(135deg,#450a0a,#7f1d1d,#991b1b);
  border:1px solid rgba(239,68,68,0.25)}
.stat-card.blue{background:linear-gradient(135deg,#082f49,#0c4a6e,#0369a1);
  border:1px solid rgba(56,189,248,0.25)}
.card-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  margin-bottom:6px;display:flex;align-items:center;gap:5px}
.stat-card.green .card-label{color:#86efac}
.stat-card.amber .card-label{color:#fcd34d}
.stat-card.red   .card-label{color:#fca5a5}
.stat-card.blue  .card-label{color:#7dd3fc}
.card-value{font-size:44px;font-weight:800;color:#fff;line-height:1}

/* Main container */
.container{max-width:1260px;margin:0 auto;padding:0 28px 50px}
.panel{background:#fff;border-radius:14px;overflow:hidden;
  box-shadow:0 8px 40px rgba(0,0,0,0.35);color:#1e293b}
.panel-head{padding:16px 24px;border-bottom:1px solid #f1f5f9;
  display:flex;align-items:center;justify-content:space-between}
.panel-head-left{display:flex;align-items:center;gap:10px}
.panel-bar{width:4px;height:24px;background:#4f46e5;border-radius:2px}
.panel-title{font-size:17px;font-weight:700;color:#1e293b}
.panel-sub{font-size:12px;color:#94a3b8}
.panel-body{padding:20px 24px}

/* Notification cards */
.notif-list{display:flex;flex-direction:column;gap:12px}
.ncard{border-radius:10px;border-left:4px solid #6366f1;background:#f8fafc;
  padding:14px 18px;transition:box-shadow .15s}
.ncard:hover{box-shadow:0 4px 16px rgba(0,0,0,0.1)}
.ncard.REROUTE_ALERT{border-left-color:#ef4444;background:#fff5f5}
.ncard.SHIPMENT_ALERT{border-left-color:#f59e0b;background:#fffbeb}
.ncard.APPOINTMENT_RESCHEDULE{border-left-color:#3b82f6;background:#eff6ff}

.ncard-head{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.type-badge{font-size:10px;font-weight:700;padding:3px 10px;border-radius:99px;
  letter-spacing:.05em;white-space:nowrap}
.type-REROUTE_ALERT{background:#fecaca;color:#991b1b}
.type-SHIPMENT_ALERT{background:#fef3c7;color:#92400e}
.type-APPOINTMENT_RESCHEDULE{background:#dbeafe;color:#1e40af}

.risk-badge{font-size:10px;font-weight:700;padding:3px 10px;border-radius:99px}
.risk-CRITICAL{background:#fecaca;color:#991b1b}
.risk-HIGH{background:#fed7aa;color:#9a3412}
.risk-MEDIUM{background:#fef3c7;color:#92400e}
.risk-LOW{background:#dcfce7;color:#166534}

.ncard-sid{font-family:monospace;font-size:12px;font-weight:700;color:#4f46e5;
  background:#ede9fe;padding:2px 8px;border-radius:4px}
.ncard-time{font-size:11px;color:#9ca3af;margin-left:auto}

.ncard-msg{font-size:14px;color:#334155;line-height:1.55;margin-bottom:8px}

.ncard-meta{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:#64748b}
.meta-item{display:flex;align-items:center;gap:4px}
.meta-label{font-weight:600;color:#475569}

.empty{text-align:center;padding:60px 20px;color:#94a3b8;font-size:14px}
.empty-icon{font-size:48px;margin-bottom:12px;display:block;opacity:.4}

/* Footer */
.footer{text-align:center;padding:16px;color:#475569;font-size:11px;
  max-width:1260px;margin:0 auto}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <span class="logo-icon">&#x1F3E5;</span>
    <div class="topbar-title">
      <span class="main">Hospital Notification Center</span>
      <span class="sub">Pharma Cargo Monitor &bull; Logistics Ops</span>
    </div>
  </div>
  <div class="topbar-right">
    <span class="sys-pill"><span class="sys-dot"></span> System Online</span>
    <span class="refresh-time">Last refresh: <strong id="last-refresh">--:--:--</strong></span>
    <span class="live-badge">&#x25CF; LIVE</span>
  </div>
</div>

<div class="stats-row">
  <div class="stat-card green">
    <div class="card-label">&#x1F514; Total Notifications</div>
    <div class="card-value" id="stat-total">0</div>
  </div>
  <div class="stat-card red">
    <div class="card-label">&#x2708;&#xFE0F; Reroute Alerts</div>
    <div class="card-value" id="stat-reroute">0</div>
  </div>
  <div class="stat-card amber">
    <div class="card-label">&#x26A0;&#xFE0F; Shipment Alerts</div>
    <div class="card-value" id="stat-shipment">0</div>
  </div>
  <div class="stat-card blue">
    <div class="card-label">&#x1F4C5; Appt. Reschedules</div>
    <div class="card-value" id="stat-appt">0</div>
  </div>
</div>

<div class="container">
  <div class="panel">
    <div class="panel-head">
      <div class="panel-head-left">
        <span class="panel-bar"></span>
        <div>
          <div class="panel-title">Incoming Alerts</div>
          <div class="panel-sub">Real-time notifications from the agent pipeline &mdash; appear here after approval &amp; execution</div>
        </div>
      </div>
    </div>
    <div class="panel-body">
      <div class="notif-list" id="notif-list">
        <div class="empty">
          <span class="empty-icon">&#x1F514;</span>
          No notifications yet.<br/>
          Alerts will appear here when a <strong>NOTIFY_HOSPITAL</strong> or <strong>REROUTE_SHIPMENT</strong>
          action is approved and executed by the agent pipeline.
        </div>
      </div>
    </div>
  </div>
</div>

<div class="footer">
  Hospital Notification Center &bull; Pharma Cargo Monitor System &bull;
  GDP &sect;9.2 | 21 CFR 600.15 &bull; Auto-refresh 3s
</div>

<script>
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function fmtTime(iso){
  if(!iso)return'—';
  try{
    var d=new Date(iso);
    return d.toLocaleDateString()+' '+d.toLocaleTimeString();
  }catch(e){return iso;}
}

function typeBadge(t){
  var labels={'REROUTE_ALERT':'&#x2708; Reroute Alert',
              'SHIPMENT_ALERT':'&#x26A0; Shipment Alert',
              'APPOINTMENT_RESCHEDULE':'&#x1F4C5; Appt. Reschedule'};
  return '<span class="type-badge type-'+t+'">'+(labels[t]||esc(t))+'</span>';
}

function riskBadge(r){
  if(!r)return'';
  return '<span class="risk-badge risk-'+r+'">'+esc(r)+'</span>';
}

function buildCard(n){
  var t=n.notification_type||'UNKNOWN';
  var meta='';
  if(n.chosen_path)   meta+='<span class="meta-item"><span class="meta-label">Path:</span>'+esc(n.chosen_path)+'</span>';
  if(n.new_eta_hours!=null) meta+='<span class="meta-item"><span class="meta-label">ETA:</span>'+Number(n.new_eta_hours).toFixed(1)+'h</span>';
  if(n.time_to_spoilage_hours!=null) meta+='<span class="meta-item"><span class="meta-label">Time-to-Spoilage:</span>'+Number(n.time_to_spoilage_hours).toFixed(1)+'h</span>';
  if(n.margin_hours!=null){
    var mc=n.margin_hours<0?'color:#dc2626;font-weight:700':'color:#16a34a;font-weight:700';
    meta+='<span class="meta-item"><span class="meta-label">Margin:</span><span style="'+mc+'">'+Number(n.margin_hours).toFixed(1)+'h</span></span>';
  }
  if(n.cold_storage_facility) meta+='<span class="meta-item"><span class="meta-label">Facility:</span>'+esc(n.cold_storage_facility)+'</span>';
  if(n.spoilage_probability!=null) meta+='<span class="meta-item"><span class="meta-label">Spoilage:</span>'+(n.spoilage_probability*100).toFixed(0)+'%</span>';
  if(n.delay_hours!=null)  meta+='<span class="meta-item"><span class="meta-label">Delay:</span>'+Number(n.delay_hours).toFixed(1)+'h</span>';
  if(n.urgency)            meta+='<span class="meta-item"><span class="meta-label">Urgency:</span>'+esc(n.urgency)+'</span>';
  if(n.vaccination_priority&&n.vaccination_priority.tier)
    meta+='<span class="meta-item"><span class="meta-label">Demand Tier:</span>'+esc(n.vaccination_priority.tier)+'</span>';

  return '<div class="ncard '+esc(t)+'">'+
    '<div class="ncard-head">'+
      typeBadge(t)+
      (n.risk_level?riskBadge(n.risk_level):'')+
      (n.shipment_id?'<span class="ncard-sid">'+esc(n.shipment_id)+'</span>':'')+
      '<span class="ncard-time">'+fmtTime(n.received_at||n.timestamp)+'</span>'+
    '</div>'+
    '<div class="ncard-msg">'+esc(n.message||n.justification||'No message.')+'</div>'+
    (meta?'<div class="ncard-meta">'+meta+'</div>':'')+
  '</div>';
}

async function refresh(){
  try{
    var data=await(await fetch('/api/notifications')).json();
    var total=data.length;
    var reroute=data.filter(function(n){return n.notification_type==='REROUTE_ALERT';}).length;
    var shipment=data.filter(function(n){return n.notification_type==='SHIPMENT_ALERT';}).length;
    var appt=data.filter(function(n){return n.notification_type==='APPOINTMENT_RESCHEDULE';}).length;

    document.getElementById('stat-total').textContent=total;
    document.getElementById('stat-reroute').textContent=reroute;
    document.getElementById('stat-shipment').textContent=shipment;
    document.getElementById('stat-appt').textContent=appt;

    var list=document.getElementById('notif-list');
    if(!total){
      list.innerHTML='<div class="empty"><span class="empty-icon">&#x1F514;</span>No notifications yet.<br/>Alerts will appear here when a <strong>NOTIFY_HOSPITAL</strong> or <strong>REROUTE_SHIPMENT</strong> action is approved and executed.</div>';
    }else{
      list.innerHTML=data.map(buildCard).join('');
    }

    document.getElementById('last-refresh').textContent=new Date().toLocaleTimeString();
    document.title=(total>0?'('+total+') ':'')+'Hospital Notification Center';
  }catch(e){console.warn('Refresh failed:',e.message);}
}
refresh();
setInterval(refresh,3000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def hospital_dashboard():
    return HTMLResponse(content=_HTML)
