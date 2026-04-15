"""HTML template for the enhanced HITL Dashboard."""

_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  background:#f0f2f5;color:#111827;font-size:15px;line-height:1.6;min-height:100vh}

/* ── Top bar ── */
.topbar{background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#f9fafb;padding:0 32px;
  display:flex;align-items:center;justify-content:space-between;height:60px;position:sticky;top:0;z-index:100;
  box-shadow:0 2px 12px rgba(0,0,0,0.25)}
.topbar-title{font-size:18px;font-weight:700;letter-spacing:-0.01em;display:flex;align-items:center;gap:10px}
.topbar-title .logo{font-size:22px}
.topbar-title .brand{color:#60a5fa}
.topbar-title .sep{color:#475569;font-weight:400}
.topbar-meta{display:flex;align-items:center;gap:20px;font-size:13px;color:#94a3b8}
.conn-pill{display:flex;align-items:center;gap:6px;border-radius:99px;padding:4px 14px 4px 10px;
  font-size:12px;font-weight:600}
.conn-pill.ok{background:rgba(34,197,94,0.15);color:#4ade80}
.conn-pill.err{background:rgba(239,68,68,0.15);color:#f87171}
.conn-dot{width:8px;height:8px;border-radius:50%}
.conn-pill.ok .conn-dot{background:#22c55e;animation:pulse 2s infinite}
.conn-pill.err .conn-dot{background:#ef4444}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.refresh-hint{font-size:11px;color:#64748b;background:#1e293b;border:1px solid #334155;
  border-radius:6px;padding:2px 8px}
kbd{background:#334155;border:1px solid #475569;border-radius:4px;padding:1px 5px;font-size:10px;
  font-family:inherit;color:#cbd5e1}

/* ── Stats strip ── */
.stats-strip{background:#fff;border-bottom:1px solid #e2e8f0;padding:0 32px;display:flex;
  box-shadow:0 1px 3px rgba(0,0,0,0.04)}
.stat-cell{padding:16px 32px 16px 0;margin-right:32px;border-right:1px solid #f1f5f9;
  display:flex;flex-direction:column;gap:2px}
.stat-cell:last-child{border-right:none;margin-right:0}
.slabel{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#64748b}
.svalue{font-size:28px;font-weight:800;color:#1e293b;transition:color .3s}
.svalue.pend{color:#d97706}.svalue.appr{color:#16a34a}.svalue.rejt{color:#dc2626}
.svalue.tout{color:#7c3aed}.svalue.audit-c{color:#2563eb}
.stat-cell .trend{font-size:11px;color:#94a3b8;font-weight:500}

/* ── Layout ── */
.container{max-width:1280px;margin:0 auto;padding:28px 24px 80px;display:flex;flex-direction:column;gap:24px}

/* ── Panels ── */
.panel{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;
  box-shadow:0 1px 4px rgba(0,0,0,0.05);transition:box-shadow .2s}
.panel:hover{box-shadow:0 4px 16px rgba(0,0,0,0.08)}
.panel-head{padding:14px 24px;border-bottom:1px solid #f1f5f9;background:#fafbfc;
  display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.panel-head h2{font-size:15px;font-weight:700;color:#0f172a;display:flex;align-items:center;gap:8px}
.panel-head h2 .icon{font-size:18px}
.panel-head .sub{font-size:11px;color:#94a3b8;font-weight:500}
.cnt-pill{background:#e2e8f0;color:#334155;font-size:11px;font-weight:700;padding:2px 10px;border-radius:99px}
.cnt-pill.hot{background:#fef3c7;color:#92400e;animation:glow 2s infinite}
@keyframes glow{0%,100%{box-shadow:0 0 0 0 rgba(217,119,6,0)}50%{box-shadow:0 0 0 4px rgba(217,119,6,0.15)}}
.panel-body{padding:20px 24px}

/* ── Empty state ── */
.empty{text-align:center;padding:40px 20px;color:#94a3b8;font-size:14px;background:#f8fafc;
  border-radius:10px;border:1px dashed #cbd5e1}
.empty .empty-icon{font-size:32px;margin-bottom:8px;display:block}

/* ── Badges ── */
.badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:700;padding:3px 10px;
  border-radius:99px;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.risk-LOW{background:#dcfce7;color:#166534}
.risk-MEDIUM{background:#fef9c3;color:#854d0e}
.risk-HIGH{background:#fed7aa;color:#9a3412}
.risk-CRITICAL{background:#fecaca;color:#991b1b;animation:critpulse 1.5s infinite}
@keyframes critpulse{0%,100%{background:#fecaca}50%{background:#fca5a5}}
.st-APPROVED{background:#dcfce7;color:#166534}
.st-REJECTED{background:#fecaca;color:#991b1b}
.st-PARTIAL{background:#dbeafe;color:#1e40af}
.st-TIMEOUT{background:#ede9fe;color:#5b21b6}
.st-PENDING{background:#fef3c7;color:#92400e}

/* ── Approval cards ── */
.acard{border:1px solid #e2e8f0;border-radius:12px;margin-bottom:16px;background:#fff;
  box-shadow:0 1px 3px rgba(0,0,0,0.04);transition:border-color .2s,box-shadow .2s}
.acard:hover{border-color:#93c5fd;box-shadow:0 4px 12px rgba(59,130,246,0.1)}
.acard:last-child{margin-bottom:0}
.acard.crit{border-left:4px solid #ef4444}
.acard.high{border-left:4px solid #f97316}
.acard.med{border-left:4px solid #eab308}
.acard-head{display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid #f1f5f9;
  background:#fafbfc;flex-wrap:wrap}
.acard-head .sid{font-size:16px;font-weight:700;color:#0f172a;font-family:'SFMono-Regular',Consolas,monospace}
.chip{font-size:11px;color:#64748b;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:6px;padding:2px 9px}
.acard-body{padding:16px 20px}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-bottom:14px}
.mlabel{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#94a3b8;margin-bottom:2px}
.mval{font-size:14px;color:#1e293b;font-weight:600}
.mval.mono{font-family:'SFMono-Regular',Consolas,monospace;font-size:11px;color:#475569}

/* ── Score bar ── */
.score-bar{height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;margin-top:4px;width:100px}
.score-fill{height:100%;border-radius:3px;transition:width .5s ease}
.score-fill.low{background:#22c55e}.score-fill.med{background:#eab308}
.score-fill.high{background:#f97316}.score-fill.crit{background:#ef4444}

/* ── Justification ── */
.just{background:#f8fafc;border-left:4px solid #3b82f6;border-radius:0 10px 10px 0;
  padding:12px 16px;font-size:13px;color:#334155;line-height:1.7;margin-bottom:16px}

/* ── Action pills ── */
.alabel{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#64748b;margin-bottom:8px}
.apills{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}
.apill{display:flex;align-items:center;gap:6px;background:#f8fafc;border:1px solid #d1d5db;
  border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px;color:#334155;
  transition:all .15s;user-select:none}
.apill:hover{border-color:#3b82f6;background:#eff6ff}
.apill input[type=checkbox]{cursor:pointer;accent-color:#2563eb;width:15px;height:15px}

/* ── Decision row ── */
.drow{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
input[type=text]{background:#fff;border:1px solid #d1d5db;border-radius:8px;padding:9px 14px;
  font-size:14px;color:#0f172a;outline:none;transition:border-color .15s,box-shadow .15s}
input[type=text]:focus{border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,0.12)}
.iop{width:170px}.inotes{flex:1;min-width:180px}
.btn{padding:9px 22px;border-radius:8px;border:none;font-size:14px;font-weight:600;cursor:pointer;
  transition:all .15s;white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,0.15)}
.btn:active{transform:scale(.97)}
.btn-approve{background:#16a34a;color:#fff}
.btn-reject{background:#dc2626;color:#fff}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none;filter:none}

/* ── Resolved table ── */
.twrap{overflow-x:auto;border-radius:10px;border:1px solid #e2e8f0}
table{width:100%;border-collapse:collapse;font-size:13px}
thead tr{background:#0f172a}
thead th{padding:11px 16px;text-align:left;font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:#94a3b8;white-space:nowrap}
tbody tr{transition:background .15s}
tbody tr:nth-child(odd){background:#fff}
tbody tr:nth-child(even){background:#f8fafc}
tbody tr:hover{background:#eff6ff}
tbody td{padding:11px 16px;border-bottom:1px solid #f1f5f9;vertical-align:middle;color:#334155}
tbody tr:last-child td{border-bottom:none}
.tmono{font-family:'SFMono-Regular',Consolas,monospace;font-size:12px}
.tmut{font-size:12px;color:#64748b}

/* ── Audit log ── */
.actrl{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
select{background:#fff;border:1px solid #d1d5db;border-radius:8px;padding:8px 12px;font-size:13px;
  color:#334155;cursor:pointer;outline:none}
select:focus{border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,0.12)}
.search-input{width:220px;padding:8px 12px;font-size:13px;border:1px solid #d1d5db;border-radius:8px;
  outline:none;transition:border-color .15s}
.search-input:focus{border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,0.12)}
.ascroll{max-height:400px;overflow-y:auto;border-radius:10px;border:1px solid #e2e8f0}
.atable{width:100%;border-collapse:collapse;font-size:13px}
.atable thead tr{background:#0f172a}
.atable thead th{position:sticky;top:0;z-index:5;padding:10px 14px;text-align:left;font-size:10px;
  font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#94a3b8}
.atable tbody tr:nth-child(odd){background:#fff}
.atable tbody tr:nth-child(even){background:#f8fafc}
.atable tbody tr:hover{background:#eff6ff}
.atable tbody td{padding:9px 14px;border-bottom:1px solid #f1f5f9;vertical-align:top;color:#475569}
.atable tbody tr:last-child td{border-bottom:none}
.ats{font-family:monospace;font-size:11px;color:#64748b;white-space:nowrap}
.aet{font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;white-space:nowrap}
.aet-PIPELINE_RUN{background:#dbeafe;color:#1e40af}
.aet-RISK_ASSESSMENT{background:#fef3c7;color:#92400e}
.aet-ANOMALY_DETECTED{background:#fed7aa;color:#9a3412}
.aet-HITL_DECISION{background:#e0e7ff;color:#3730a3}
.aet-ACTION_RESULT{background:#dcfce7;color:#166534}
.aet-COMPLIANCE_VIOLATION{background:#fecaca;color:#991b1b}
.asid{font-family:monospace;font-size:12px;color:#059669;font-weight:600}
.apay{font-family:monospace;font-size:11px;color:#64748b;max-width:420px;word-break:break-all;
  white-space:pre-wrap;cursor:pointer;transition:color .15s}
.apay:hover{color:#1e293b}
.apay-expand{display:none;margin-top:6px;padding:8px 10px;background:#f1f5f9;border-radius:6px;
  font-size:11px;max-height:200px;overflow-y:auto}
.apay.open .apay-expand{display:block}
.apay.open .apay-preview{color:#3b82f6}

/* ── Toast ── */
#toast{position:fixed;bottom:28px;right:28px;border-radius:12px;padding:14px 24px;font-size:14px;
  font-weight:500;pointer-events:none;opacity:0;transform:translateY(12px);transition:all .3s ease;
  z-index:999;box-shadow:0 8px 24px rgba(0,0,0,0.2);display:flex;align-items:center;gap:8px}
#toast.show{opacity:1;transform:translateY(0)}
#toast.success{background:#14532d;color:#dcfce7}
#toast.error{background:#7f1d1d;color:#fecaca}
#toast.info{background:#1e293b;color:#e2e8f0}

/* ── Modal ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:200;display:flex;
  align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .2s}
.modal-overlay.open{opacity:1;pointer-events:auto}
.modal{background:#fff;border-radius:16px;max-width:640px;width:90%;max-height:80vh;overflow-y:auto;
  box-shadow:0 20px 60px rgba(0,0,0,0.3);padding:0}
.modal-head{padding:18px 24px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;
  justify-content:space-between;background:#fafbfc;border-radius:16px 16px 0 0}
.modal-head h3{font-size:16px;font-weight:700;color:#0f172a}
.modal-close{background:none;border:none;font-size:20px;cursor:pointer;color:#64748b;padding:4px 8px;
  border-radius:6px;transition:background .15s}
.modal-close:hover{background:#f1f5f9;color:#0f172a}
.modal-body{padding:20px 24px}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.detail-item .dlabel{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
  color:#94a3b8;margin-bottom:4px}
.detail-item .dval{font-size:14px;color:#1e293b;font-weight:500}

/* ── Responsive ── */
@media(max-width:768px){
  .topbar{padding:0 16px;height:52px}
  .topbar-title{font-size:15px}
  .stats-strip{padding:0 16px;flex-wrap:wrap}
  .stat-cell{padding:10px 16px 10px 0;margin-right:16px}
  .svalue{font-size:22px}
  .container{padding:16px 12px 60px}
  .panel-body{padding:14px 16px}
  .meta-grid{grid-template-columns:1fr 1fr}
  .drow{flex-direction:column;align-items:stretch}
  .iop,.inotes{width:100%}
  .refresh-hint{display:none}
  .detail-grid{grid-template-columns:1fr}
}
"""

_BODY = """
<div class="topbar">
  <div class="topbar-title">
    <span class="logo">&#x1F48A;</span>
    <span><span class="brand">Pharma Cargo Monitor</span> <span class="sep">|</span> HITL Dashboard</span>
  </div>
  <div class="topbar-meta">
    <span>Refreshed: <strong id="last-refresh" style="color:#e2e8f0">&#8212;</strong></span>
    <span class="conn-pill ok" id="conn-pill"><span class="conn-dot"></span><span id="conn-text">Connected</span></span>
    <span class="refresh-hint"><kbd>R</kbd> refresh &middot; <kbd>Esc</kbd> close</span>
  </div>
</div>

<div class="stats-strip">
  <div class="stat-cell">
    <div class="slabel">&#9888;&#65039; Pending</div>
    <div class="svalue pend" id="stat-pending">&#8212;</div>
  </div>
  <div class="stat-cell">
    <div class="slabel">&#9989; Approved</div>
    <div class="svalue appr" id="stat-approved">&#8212;</div>
  </div>
  <div class="stat-cell">
    <div class="slabel">&#10060; Rejected</div>
    <div class="svalue rejt" id="stat-rejected">&#8212;</div>
  </div>
  <div class="stat-cell">
    <div class="slabel">&#9203; Timed Out</div>
    <div class="svalue tout" id="stat-timeout">&#8212;</div>
  </div>
  <div class="stat-cell">
    <div class="slabel">&#128220; Audit Events</div>
    <div class="svalue audit-c" id="stat-audit">&#8212;</div>
  </div>
</div>

<div class="container">
  <!-- Pending Approvals -->
  <div class="panel" id="panel-pending">
    <div class="panel-head">
      <h2><span class="icon">&#128203;</span> Pending Approvals</h2>
      <span class="cnt-pill hot" id="badge-pending">0</span>
      <span class="sub">sorted by risk (highest first)</span>
    </div>
    <div class="panel-body">
      <div id="pending-list">
        <div class="empty"><span class="empty-icon">&#8987;</span>Loading&#8230;</div>
      </div>
    </div>
  </div>

  <!-- Resolved Decisions -->
  <div class="panel">
    <div class="panel-head">
      <h2><span class="icon">&#9989;</span> Resolved Decisions</h2>
      <span class="cnt-pill" id="badge-resolved">0</span>
      <span class="sub">most recent first</span>
    </div>
    <div class="panel-body">
      <div id="resolved-wrap">
        <div class="empty"><span class="empty-icon">&#128196;</span>No resolved decisions yet.</div>
      </div>
    </div>
  </div>

  <!-- Audit Log -->
  <div class="panel">
    <div class="panel-head">
      <h2><span class="icon">&#128209;</span> Audit Log</h2>
      <span class="sub">ALCOA+ &mdash; GDP &sect;8 / 21 CFR 211.68</span>
      <span class="cnt-pill" id="badge-audit">0</span>
    </div>
    <div class="panel-body">
      <div class="actrl">
        <select id="audit-n" aria-label="Number of records">
          <option value="25">Last 25 records</option>
          <option value="50" selected>Last 50 records</option>
          <option value="100">Last 100 records</option>
          <option value="200">Last 200 records</option>
        </select>
        <select id="audit-type" aria-label="Event type filter">
          <option value="">All event types</option>
          <option value="PIPELINE_RUN">PIPELINE_RUN</option>
          <option value="RISK_ASSESSMENT">RISK_ASSESSMENT</option>
          <option value="ANOMALY_DETECTED">ANOMALY_DETECTED</option>
          <option value="HITL_DECISION">HITL_DECISION</option>
          <option value="ACTION_RESULT">ACTION_RESULT</option>
          <option value="COMPLIANCE_VIOLATION">COMPLIANCE_VIOLATION</option>
        </select>
        <input type="text" class="search-input" id="audit-search" placeholder="Search shipment ID..." aria-label="Search audit log">
      </div>
      <div class="ascroll">
        <table class="atable">
          <thead><tr>
            <th>Timestamp (UTC)</th><th>Event Type</th><th>Shipment ID</th><th>Payload</th>
          </tr></thead>
          <tbody id="audit-body">
            <tr><td colspan="4" style="text-align:center;padding:28px;color:#94a3b8">
              <span style="font-size:24px;display:block;margin-bottom:4px">&#128220;</span>Loading&#8230;
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="toast"></div>

<!-- Detail Modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <h3 id="modal-title">Shipment Details</h3>
      <button class="modal-close" id="modal-close" aria-label="Close">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>
"""

_JS = r"""
const RL=['LOW','MEDIUM','HIGH','CRITICAL'];
let _auditCache=[];
let _refreshing=false;

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function badge(c,t){return '<span class="badge '+c+'">'+t+'</span>';}
function riskBadge(l){
  const icons={LOW:'\u2705',MEDIUM:'\u26A0\uFE0F',HIGH:'\uD83D\uDD25',CRITICAL:'\uD83D\uDEA8'};
  return badge('risk-'+l,(icons[l]||'')+' '+l);
}
function statusBadge(s){
  const icons={APPROVED:'\u2705',REJECTED:'\u274C',PARTIAL:'\u2796',TIMEOUT:'\u23F3',PENDING:'\u23F1\uFE0F'};
  return badge('st-'+s,(icons[s]||'')+' '+s);
}
function fmtTime(iso){
  if(!iso)return'\u2014';
  return new Date(iso).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function fmtDate(iso){
  if(!iso)return'\u2014';
  var d=new Date(iso);
  return d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function scoreClass(s){return s>=0.85?'crit':s>=0.7?'high':s>=0.4?'med':'low';}
function cardClass(l){return l==='CRITICAL'?'crit':l==='HIGH'?'high':l==='MEDIUM'?'med':'';}

function toast(msg,type){
  type=type||'success';
  var el=document.getElementById('toast');
  el.innerHTML=(type==='success'?'\u2705':type==='error'?'\u274C':'\u2139\uFE0F')+' '+esc(msg);
  el.className='show '+type;
  setTimeout(function(){el.className='';},3500);
}

function setConn(ok){
  var pill=document.getElementById('conn-pill');
  var txt=document.getElementById('conn-text');
  if(ok){pill.className='conn-pill ok';txt.textContent='Connected';}
  else{pill.className='conn-pill err';txt.textContent='Disconnected';}
}

function buildPendingCard(req){
  var cnt=(req.proposed_actions||[]).length;
  var sc=req.risk_score;
  var cls=cardClass(req.risk_level);
  var pills='';
  for(var i=0;i<(req.proposed_actions||[]).length;i++){
    var a=req.proposed_actions[i];
    pills+='<label class="apill"><input type="checkbox" name="act_'+req.request_id+'" value="'+a+'" checked>'+a.replace(/_/g,' ')+'</label>';
  }
  var elapsed='';
  if(req.created_at){
    var secs=Math.floor((Date.now()-new Date(req.created_at).getTime())/1000);
    if(secs<60)elapsed=secs+'s ago';
    else if(secs<3600)elapsed=Math.floor(secs/60)+'m ago';
    else elapsed=Math.floor(secs/3600)+'h ago';
  }

  var html='<div class="acard '+cls+'" id="card-'+req.request_id+'">';
  html+='<div class="acard-head">';
  html+='<span class="sid">'+esc(req.shipment_id)+'</span>';
  html+=riskBadge(req.risk_level);
  html+='<span class="chip">Score: '+sc.toFixed(2)+'</span>';
  html+='<span class="chip">\u23F1\uFE0F '+elapsed+'</span>';
  html+='<span class="chip">'+cnt+' action'+(cnt!==1?'s':'')+'</span>';
  html+='</div>';

  html+='<div class="acard-body">';
  html+='<div class="meta-grid">';
  html+='<div><div class="mlabel">Request ID</div><div class="mval mono">'+req.request_id.slice(0,8)+'&hellip;</div></div>';
  html+='<div><div class="mlabel">Risk Score</div><div class="mval">'+sc.toFixed(4);
  html+='<div class="score-bar"><div class="score-fill '+scoreClass(sc)+'" style="width:'+Math.round(sc*100)+'%"></div></div>';
  html+='</div></div>';
  html+='<div><div class="mlabel">Risk Level</div><div class="mval">'+riskBadge(req.risk_level)+'</div></div>';
  html+='<div><div class="mlabel">Submitted</div><div class="mval">'+fmtTime(req.created_at)+'</div></div>';
  html+='</div>';

  html+='<div class="just">'+(req.justification||'No justification provided.')+'</div>';

  html+='<div class="alabel">Select actions to approve</div>';
  html+='<div class="apills">'+(pills||'<span style="color:#94a3b8;font-size:13px">No actions proposed</span>')+'</div>';

  html+='<div class="drow">';
  html+='<input class="iop" type="text" id="op-'+req.request_id+'" placeholder="Operator name">';
  html+='<input class="inotes" type="text" id="nt-'+req.request_id+'" placeholder="Notes (optional)">';
  html+='<button class="btn btn-approve" onclick="decide(\''+req.request_id+'\',\'approve\')">\u2713 Approve</button>';
  html+='<button class="btn btn-reject" onclick="decide(\''+req.request_id+'\',\'reject\')">\u2717 Reject</button>';
  html+='</div>';

  html+='</div></div>';
  return html;
}

function buildResolvedTable(items){
  if(!items.length)return '<div class="empty"><span class="empty-icon">\uD83D\uDCC4</span>No resolved decisions yet.</div>';
  var rows='';
  for(var i=0;i<items.length;i++){
    var r=items[i];
    var acts=(r.approved_actions||[]).map(function(a){return a.replace(/_/g,' ');}).join(', ')||'\u2014';
    rows+='<tr style="cursor:pointer" onclick="showDetail('+i+')">';
    rows+='<td style="font-weight:700;font-family:monospace">'+esc(r.shipment_id)+'</td>';
    rows+='<td>'+riskBadge(r.risk_level)+'</td>';
    rows+='<td class="tmono">'+r.risk_score.toFixed(4)+'</td>';
    rows+='<td>'+statusBadge(r.status)+'</td>';
    rows+='<td class="tmut" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(acts)+'</td>';
    rows+='<td class="tmut">'+(r.decided_by||'auto')+'</td>';
    rows+='<td class="tmut">'+fmtTime(r.decided_at)+'</td>';
    rows+='</tr>';
  }
  return '<div class="twrap"><table><thead><tr><th>Shipment</th><th>Risk</th><th>Score</th><th>Status</th><th>Approved Actions</th><th>Decided By</th><th>Time</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}

var _resolvedItems=[];
function showDetail(idx){
  var r=_resolvedItems[idx];
  if(!r)return;
  var acts=(r.approved_actions||[]).map(function(a){return a.replace(/_/g,' ');}).join(', ')||'None';
  document.getElementById('modal-title').textContent=r.shipment_id+' \u2014 Details';
  var html='<div class="detail-grid">';
  html+='<div class="detail-item"><div class="dlabel">Shipment ID</div><div class="dval" style="font-family:monospace">'+esc(r.shipment_id)+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Request ID</div><div class="dval" style="font-family:monospace;font-size:12px">'+esc(r.request_id)+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Risk Level</div><div class="dval">'+riskBadge(r.risk_level)+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Risk Score</div><div class="dval">'+r.risk_score.toFixed(4)+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Status</div><div class="dval">'+statusBadge(r.status)+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Decided By</div><div class="dval">'+(r.decided_by||'auto')+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Decision Time</div><div class="dval">'+fmtDate(r.decided_at)+'</div></div>';
  html+='<div class="detail-item"><div class="dlabel">Created</div><div class="dval">'+fmtDate(r.created_at)+'</div></div>';
  html+='</div>';
  html+='<div style="margin-top:16px"><div class="dlabel">Approved Actions</div><div style="margin-top:6px">'+esc(acts)+'</div></div>';
  html+='<div style="margin-top:12px"><div class="dlabel">Justification</div><div class="just" style="margin-top:6px">'+(r.justification||'N/A')+'</div></div>';
  if(r.notes){html+='<div style="margin-top:8px"><div class="dlabel">Notes</div><div style="margin-top:4px;color:#475569">'+esc(r.notes)+'</div></div>';}
  document.getElementById('modal-body').innerHTML=html;
  document.getElementById('modal-overlay').classList.add('open');
}

function closeModal(){document.getElementById('modal-overlay').classList.remove('open');}
document.getElementById('modal-close').addEventListener('click',closeModal);
document.getElementById('modal-overlay').addEventListener('click',function(e){
  if(e.target===this)closeModal();
});

function buildAuditTable(records){
  var body=document.getElementById('audit-body');
  if(!records.length){
    body.innerHTML='<tr><td colspan="4" style="text-align:center;padding:32px;color:#94a3b8"><span style="font-size:24px;display:block;margin-bottom:4px">\uD83D\uDCDC</span>No audit records found.</td></tr>';
    return;
  }
  var search=(document.getElementById('audit-search').value||'').trim().toLowerCase();
  var filtered=records;
  if(search){
    filtered=records.filter(function(r){
      return (r.shipment_id||'').toLowerCase().indexOf(search)!==-1 ||
             (r.event_type||'').toLowerCase().indexOf(search)!==-1 ||
             JSON.stringify(r.payload||{}).toLowerCase().indexOf(search)!==-1;
    });
  }
  if(!filtered.length){
    body.innerHTML='<tr><td colspan="4" style="text-align:center;padding:24px;color:#94a3b8">No matching records.</td></tr>';
    return;
  }
  var html='';
  for(var i=0;i<filtered.length;i++){
    var r=filtered[i];
    var p=JSON.stringify(r.payload||{},null,2);
    var preview=p.length>200?p.slice(0,200)+'\u2026':p;
    var et=r.event_type||'UNKNOWN';
    html+='<tr>';
    html+='<td class="ats">'+fmtDate(r.timestamp)+'</td>';
    html+='<td><span class="aet aet-'+et+'">'+et+'</span></td>';
    html+='<td class="asid">'+(r.shipment_id||'\u2014')+'</td>';
    html+='<td><div class="apay" onclick="this.classList.toggle(\'open\')">';
    html+='<div class="apay-preview">'+esc(preview)+'</div>';
    if(p.length>200){html+='<div class="apay-expand">'+esc(p)+'</div>';}
    html+='</div></td>';
    html+='</tr>';
  }
  body.innerHTML=html;
}

async function decide(requestId,action){
  var opEl=document.getElementById('op-'+requestId);
  var ntEl=document.getElementById('nt-'+requestId);
  var operator=(opEl?opEl.value:'').trim()||'operator';
  var notes=(ntEl?ntEl.value:'').trim();
  var body={operator:operator,notes:notes};
  if(action==='approve'){
    var checks=document.querySelectorAll('input[name="act_'+requestId+'"]:checked');
    body.approved_actions=[];
    for(var i=0;i<checks.length;i++)body.approved_actions.push(checks[i].value);
  }
  // Disable buttons
  var card=document.getElementById('card-'+requestId);
  if(card){var btns=card.querySelectorAll('.btn');for(var i=0;i<btns.length;i++)btns[i].disabled=true;}
  try{
    var resp=await fetch('/queue/'+requestId+'/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!resp.ok){var err=await resp.json();throw new Error(err.detail||resp.statusText);}
    toast(action==='approve'?'Actions approved successfully':'Request rejected','success');
    await refresh();
  }catch(e){
    toast('Error: '+e.message,'error');
    if(card){var btns=card.querySelectorAll('.btn');for(var i=0;i<btns.length;i++)btns[i].disabled=false;}
  }
}

async function fetchAudit(){
  var n=document.getElementById('audit-n').value;
  var t=document.getElementById('audit-type').value;
  var url='/audit?n='+n;
  if(t)url+='&event_type='+encodeURIComponent(t);
  try{
    var data=await(await fetch(url)).json();
    _auditCache=data;
    document.getElementById('stat-audit').textContent=data.length;
    document.getElementById('badge-audit').textContent=data.length;
    buildAuditTable(data);
  }catch(e){
    document.getElementById('audit-body').innerHTML='<tr><td colspan="4" style="color:#dc2626;padding:14px">\u274C Audit fetch failed: '+esc(e.message)+'</td></tr>';
  }
}

async function refresh(){
  if(_refreshing)return;
  _refreshing=true;
  try{
    var all=await(await fetch('/queue/all')).json();
    setConn(true);
    var pending=all.filter(function(r){return r.status==='PENDING';}).sort(function(a,b){return RL.indexOf(b.risk_level)-RL.indexOf(a.risk_level);});
    var resolved=all.filter(function(r){return r.status!=='PENDING';}).sort(function(a,b){return(b.decided_at||b.created_at).localeCompare(a.decided_at||a.created_at);});
    _resolvedItems=resolved;

    var nAppr=all.filter(function(r){return r.status==='APPROVED'||r.status==='PARTIAL';}).length;
    var nRejt=all.filter(function(r){return r.status==='REJECTED';}).length;
    var nTout=all.filter(function(r){return r.status==='TIMEOUT';}).length;

    document.getElementById('stat-pending').textContent=pending.length;
    document.getElementById('stat-approved').textContent=nAppr;
    document.getElementById('stat-rejected').textContent=nRejt;
    document.getElementById('stat-timeout').textContent=nTout;
    document.getElementById('badge-pending').textContent=pending.length;
    document.getElementById('badge-resolved').textContent=resolved.length;

    if(pending.length){
      document.getElementById('pending-list').innerHTML=pending.map(buildPendingCard).join('');
      document.title='('+pending.length+') Pharma Cargo Monitor - HITL Dashboard';
    }else{
      document.getElementById('pending-list').innerHTML='<div class="empty"><span class="empty-icon">\u2705</span>No pending approvals \u2014 all shipments are clear.</div>';
      document.title='Pharma Cargo Monitor - HITL Dashboard';
    }
    document.getElementById('resolved-wrap').innerHTML=buildResolvedTable(resolved);
    document.getElementById('last-refresh').textContent=new Date().toLocaleTimeString();
  }catch(e){
    setConn(false);
    console.warn('Refresh failed:',e.message);
  }
  _refreshing=false;
  await fetchAudit();
}

// Event listeners
document.getElementById('audit-n').addEventListener('change',fetchAudit);
document.getElementById('audit-type').addEventListener('change',fetchAudit);
document.getElementById('audit-search').addEventListener('input',function(){buildAuditTable(_auditCache);});

// Keyboard shortcuts
document.addEventListener('keydown',function(e){
  if(e.key==='Escape')closeModal();
  if(e.key==='r'&&!e.ctrlKey&&!e.metaKey&&document.activeElement.tagName!=='INPUT'&&document.activeElement.tagName!=='SELECT'){
    e.preventDefault();refresh();toast('Refreshed','info');
  }
});

// Start
refresh();
setInterval(refresh,3000);
"""


def get_dashboard_html() -> str:
    """Return the complete HTML for the HITL dashboard."""
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">\n'
        '<title>Pharma Cargo Monitor - HITL Dashboard</title>\n'
        '<style>' + _CSS + '</style>\n'
        '</head>\n'
        '<body>\n'
        + _BODY +
        '\n<script>' + _JS + '</script>\n'
        '</body>\n'
        '</html>'
    )
