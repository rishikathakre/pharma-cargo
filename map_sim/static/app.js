let map;
let routeLine = null;
let shipmentMarker = null;
let originMarker = null;
let destMarker = null;
let previewLine = null;
let originWeatherCircle = null;
let destWeatherCircle = null;
let pollTimer = null;
let activeSimId = null;
let sidebarTimer = null;
let followActive = false;
const MAX_WEATHER_RADIUS_KM = 45;

const statusEl = document.getElementById("status");
const activeInfoEl = document.getElementById("activeInfo");
const shipmentListEl = document.getElementById("shipmentList");
const hitlAlertsEl = document.getElementById("hitlAlerts");
const activeHintEl = document.getElementById("activeHint");
const originSearchEl = document.getElementById("originSearch");
const destSearchEl = document.getElementById("destSearch");

let originOptionsAll = [];
let destOptionsAll = [];
const MAX_OPTIONS_RENDERED = 80;

function setStatus(msg) {
  statusEl.textContent = msg;
}

function setActiveInfo(obj) {
  if (!obj) {
    activeInfoEl.textContent = "None";
    return;
  }
  const compact = {
    shipment_id: obj.shipment_id,
    phase: obj.phase,
    risk_level: obj.risk_level,
    risk_score: obj.risk_score,
    anomalies: obj.anomalies,
    hitl_pending: obj.hitl_pending,
    lat: obj.lat,
    lon: obj.lon,
    destination: obj.destination,
  };
  activeInfoEl.textContent = JSON.stringify(compact, null, 2);
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const detail = body?.detail ? body.detail : text;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return body;
}

function initMap() {
  map = L.map("map", { worldCopyJump: true }).setView([20, 0], 2);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
}

function fillSelect(selectEl, values, placeholder = "Select an airport…") {
  selectEl.innerHTML = "";
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = placeholder;
  ph.disabled = true;
  ph.selected = true;
  selectEl.appendChild(ph);
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    selectEl.appendChild(opt);
  }
}

function filterOptions(values, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return values.slice(0, MAX_OPTIONS_RENDERED);
  const out = [];
  for (const v of values) {
    if (String(v).toLowerCase().includes(q)) out.push(v);
    if (out.length >= MAX_OPTIONS_RENDERED) break;
  }
  return out;
}

function refreshAirportSelects() {
  const originSel = document.getElementById("originSelect");
  const destSel = document.getElementById("destSelect");

  const originCurrent = originSel?.value || "";
  const destCurrent = destSel?.value || "";

  const originVals = filterOptions(originOptionsAll, originSearchEl?.value);
  const destVals = filterOptions(destOptionsAll, destSearchEl?.value);

  if (originCurrent && !originVals.includes(originCurrent) && originOptionsAll.includes(originCurrent)) {
    originVals.unshift(originCurrent);
  }
  if (destCurrent && !destVals.includes(destCurrent) && destOptionsAll.includes(destCurrent)) {
    destVals.unshift(destCurrent);
  }

  fillSelect(originSel, originVals, `Select an origin airport… (${originOptionsAll.length} available)`);
  fillSelect(destSel, destVals, `Select a destination airport… (${destOptionsAll.length} available)`);

  if (originCurrent && originOptionsAll.includes(originCurrent)) originSel.value = originCurrent;
  if (destCurrent && destOptionsAll.includes(destCurrent)) destSel.value = destCurrent;
}

function toRad(x) {
  return (x * Math.PI) / 180;
}

function toDeg(x) {
  return (x * 180) / Math.PI;
}

function greatCircleArc(a, b, segments = 48) {
  // a/b are [lat, lon] in degrees. Returns list of [lat, lon] along a great-circle.
  const lat1 = toRad(a[0]);
  const lon1 = toRad(a[1]);
  const lat2 = toRad(b[0]);
  const lon2 = toRad(b[1]);

  const d =
    2 *
    Math.asin(
      Math.sqrt(
        Math.sin((lat2 - lat1) / 2) ** 2 +
          Math.cos(lat1) * Math.cos(lat2) * Math.sin((lon2 - lon1) / 2) ** 2
      )
    );

  if (!Number.isFinite(d) || d === 0) return [a, b];

  const out = [];
  for (let i = 0; i <= segments; i++) {
    const f = i / segments;
    const A = Math.sin((1 - f) * d) / Math.sin(d);
    const B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
    const y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
    const z = A * Math.sin(lat1) + B * Math.sin(lat2);
    const lat = Math.atan2(z, Math.sqrt(x * x + y * y));
    const lon = Math.atan2(y, x);
    out.push([toDeg(lat), toDeg(lon)]);
  }
  return out;
}

async function loadOptions() {
  const opts = await fetchJSON("/api/options");
  originOptionsAll = Array.isArray(opts.origins) ? opts.origins : [];
  destOptionsAll = Array.isArray(opts.destinations) ? opts.destinations : [];
  refreshAirportSelects();
  setStatus(`Loaded ${opts.routes_count} routes (airports indexed: ${originOptionsAll.length}). Use search to filter.`);
}

function clearRoute() {
  if (routeLine) {
    map.removeLayer(routeLine);
    routeLine = null;
  }
  if (shipmentMarker) {
    map.removeLayer(shipmentMarker);
    shipmentMarker = null;
  }
  if (previewLine) {
    map.removeLayer(previewLine);
    previewLine = null;
  }
  if (originMarker) {
    map.removeLayer(originMarker);
    originMarker = null;
  }
  if (destMarker) {
    map.removeLayer(destMarker);
    destMarker = null;
  }
  if (originWeatherCircle) {
    map.removeLayer(originWeatherCircle);
    originWeatherCircle = null;
  }
  if (destWeatherCircle) {
    map.removeLayer(destWeatherCircle);
    destWeatherCircle = null;
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (sidebarTimer) {
    clearInterval(sidebarTimer);
    sidebarTimer = null;
  }
  activeSimId = null;
}

function parseNum(id) {
  const v = document.getElementById(id).value.trim();
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function setInput(id, value) {
  const el = document.getElementById(id);
  el.value = value === null || value === undefined ? "" : String(value);
}

async function lookupAndFill(kind) {
  // kind: "origin" | "dest"
  const selectId = kind === "origin" ? "originSelect" : "destSelect";
  const latId = kind === "origin" ? "originLat" : "destLat";
  const lonId = kind === "origin" ? "originLon" : "destLon";

  const name = document.getElementById(selectId).value;
  if (!name) return;
  try {
    const res = await fetchJSON(`/api/lookup?name=${encodeURIComponent(name)}`);
    if (res.found) {
      setInput(latId, res.lat.toFixed(6));
      setInput(lonId, res.lon.toFixed(6));
      setStatus(`Resolved ${name} → (${res.lat.toFixed(4)}, ${res.lon.toFixed(4)}) from ${res.source}`);
    } else {
      // Don't wipe manual values; just inform the user.
      setStatus(`No coords for "${name}". Paste lat/lon or add data/raw/airports.csv.`);
    }
  } catch (e) {
    setStatus(`Lookup failed: ${e.message}`);
  }
  drawPreview();
}

function drawPreview() {
  // Draw origin/destination markers and a preview line if we have coords for both.
  if (routeLine || shipmentMarker) return; // don't overwrite an active simulation view

  const oLat = parseNum("originLat");
  const oLon = parseNum("originLon");
  const dLat = parseNum("destLat");
  const dLon = parseNum("destLon");

  if (originMarker) {
    map.removeLayer(originMarker);
    originMarker = null;
  }
  if (destMarker) {
    map.removeLayer(destMarker);
    destMarker = null;
  }
  if (previewLine) {
    map.removeLayer(previewLine);
    previewLine = null;
  }

  if (oLat !== null && oLon !== null) {
    originMarker = L.circleMarker([oLat, oLon], {
      radius: 7,
      color: "#0ea5e9",
      fillColor: "#38bdf8",
      fillOpacity: 0.9,
      weight: 2,
    }).addTo(map);
    originMarker.bindTooltip("Origin", { direction: "top" });
  }
  if (dLat !== null && dLon !== null) {
    destMarker = L.circleMarker([dLat, dLon], {
      radius: 7,
      color: "#16a34a",
      fillColor: "#4ade80",
      fillOpacity: 0.9,
      weight: 2,
    }).addTo(map);
    destMarker.bindTooltip("Destination", { direction: "top" });
  }

  if (oLat !== null && oLon !== null && dLat !== null && dLon !== null) {
    previewLine = L.polyline(greatCircleArc([oLat, oLon], [dLat, dLon], 36), {
      color: "#94a3b8",
      weight: 3,
      opacity: 0.8,
      dashArray: "6 8",
    }).addTo(map);
    map.fitBounds(previewLine.getBounds(), { padding: [40, 40] });
  } else if (oLat !== null && oLon !== null) {
    map.setView([oLat, oLon], 5);
  } else if (dLat !== null && dLon !== null) {
    map.setView([dLat, dLon], 5);
  }
}

async function startSim() {
  stopPolling();
  clearRoute();

  const origin = document.getElementById("originSelect").value;
  const destination = document.getElementById("destSelect").value;
  if (!origin || !destination) {
    setStatus("Start failed: select both origin and destination airports.");
    return;
  }
  if (origin === destination) {
    setStatus("Start failed: origin and destination cannot be the same.");
    return;
  }
  const durationSeconds = Number(document.getElementById("durationSeconds").value || "45");
  const speedMultiplier = Number(document.getElementById("speedMultiplier").value || "60");

  const payload = {
    origin,
    destination,
    origin_lat: parseNum("originLat"),
    origin_lon: parseNum("originLon"),
    destination_lat: parseNum("destLat"),
    destination_lon: parseNum("destLon"),
    duration_seconds: durationSeconds,
    speed_multiplier: speedMultiplier,
    origin_weather: document.getElementById("originWeather")?.value || "RANDOM",
    destination_weather: document.getElementById("destWeather")?.value || "RANDOM",
  };

  let sim;
  try {
    sim = await fetchJSON("/api/sim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    setStatus(`Start failed: ${e.message}`);
    return;
  }

  activeSimId = sim.sim_id;
  setStatus(`Started ${sim.shipment_id}: ${sim.origin} → ${sim.destination}`);

  const originLatLng = [sim.origin_lat, sim.origin_lon];
  const destLatLng = [sim.destination_lat, sim.destination_lon];

  // Weather zones (dummy) around origin/destination
  const oW = sim.weather_origin;
  const dW = sim.weather_destination;
  originWeatherCircle = L.circle(originLatLng, {
    radius: Math.min(MAX_WEATHER_RADIUS_KM, oW.radius_km || MAX_WEATHER_RADIUS_KM) * 1000,
    color: oW.color || "#64748b",
    fillColor: oW.color || "#64748b",
    fillOpacity: 0.15,
    weight: 2,
  }).addTo(map);
  originWeatherCircle.bindTooltip(`Origin weather: ${oW.label}`, { sticky: true });

  destWeatherCircle = L.circle(destLatLng, {
    radius: Math.min(MAX_WEATHER_RADIUS_KM, dW.radius_km || MAX_WEATHER_RADIUS_KM) * 1000,
    color: dW.color || "#64748b",
    fillColor: dW.color || "#64748b",
    fillOpacity: 0.15,
    weight: 2,
  }).addTo(map);
  destWeatherCircle.bindTooltip(`Destination weather: ${dW.label}`, { sticky: true });

  const routePts = Array.isArray(sim.route_points) && sim.route_points.length ? sim.route_points : greatCircleArc(originLatLng, destLatLng, 64);
  routeLine = L.polyline(routePts, {
    color: "#2563eb",
    weight: 4,
    opacity: 0.85,
  }).addTo(map);

  shipmentMarker = L.circleMarker(originLatLng, {
    radius: 8,
    color: "#64748b",
    fillColor: "#94a3b8",
    fillOpacity: 0.9,
    weight: 2,
  }).addTo(map);

  shipmentMarker.bindPopup("Click marker to focus shipment").openPopup();
  shipmentMarker.on("click", () => {
    if (routeLine) {
      map.fitBounds(routeLine.getBounds(), { padding: [40, 40] });
    }
    followActive = !followActive;
    setStatus(followActive ? "Follow mode: ON (map tracks shipment)" : "Follow mode: OFF");
    shipmentMarker.openPopup();
  });

  map.fitBounds(routeLine.getBounds(), { padding: [40, 40] });

  function riskColor(level) {
    return (
      {
        CRITICAL: "#dc2626",
        HIGH: "#f97316",
        MEDIUM: "#eab308",
        LOW: "#22c55e",
      }[String(level || "LOW").toUpperCase()] || "#64748b"
    );
  }

  function badgeClass(level) {
    const v = String(level || "").toLowerCase();
    if (v === "critical") return "critical";
    if (v === "high") return "high";
    if (v === "medium") return "medium";
    if (v === "low") return "low";
    return "";
  }

  function renderShipments(sims) {
    if (!shipmentListEl) return;
    if (!Array.isArray(sims) || sims.length === 0) {
      shipmentListEl.innerHTML = '<div class="small">No active simulations.</div>';
      return;
    }
    shipmentListEl.innerHTML = sims
      .slice(0, 8)
      .map((s) => {
        const topAnom = (s.anomalies && s.anomalies.length ? s.anomalies[0] : "—").replace(/_/g, " ");
        const level = s.risk_level || "—";
        const score = s.risk_score ?? "—";
        return (
          '<div class="shipRow" onclick="window.__focusSim && window.__focusSim(\'' +
          s.sim_id +
          "')\">" +
          '<div class="shipTop">' +
          '<div class="shipId">' +
          s.shipment_id +
          "</div>" +
          '<span class="badge ' +
          badgeClass(level) +
          '">' +
          String(level) +
          "</span>" +
          "</div>" +
          '<div class="shipMeta">' +
          "Phase: <b>" +
          (s.phase || "—") +
          "</b><br>" +
          "Risk score: <b>" +
          score +
          "</b><br>" +
          "Top anomaly: <b>" +
          topAnom +
          "</b>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  function renderHitl(reqs) {
    if (!hitlAlertsEl) return;
    if (!Array.isArray(reqs) || reqs.length === 0) {
      hitlAlertsEl.innerHTML = '<div class="small">No pending HITL approvals.</div>';
      return;
    }
    hitlAlertsEl.innerHTML = reqs
      .slice(0, 3)
      .map((r) => {
        const level = r.risk_level || "HIGH";
        const cls = badgeClass(level);
        const actions = (r.proposed_actions || []).slice(0, 4).map((a) => a.replace(/_/g, " ")).join(", ");
        const justFull = String(r.justification || "");
        const just = justFull.slice(0, 160).replace(/</g, "&lt;").replace(/>/g, "&gt;");
        return (
          '<div class="alertCard ' +
          cls +
          '">' +
          "<b>" +
          r.shipment_id +
          "</b> — " +
          String(level) +
          " RISK (" +
          (r.risk_score ?? "—") +
          ")<br>" +
          '<span class="small">' +
          (actions || "—") +
          "</span><br>" +
          '<em class="small">"' +
          just +
          (justFull.length > 160 ? "…" : "") +
          '"</em>' +
          '<div class="alertActions">' +
          '<button class="btnApprove" onclick="window.__hitlApprove && window.__hitlApprove(\'' +
          r.request_id +
          "')\">✓ Approve</button>" +
          '<button class="btnReject" onclick="window.__hitlReject && window.__hitlReject(\'' +
          r.request_id +
          "')\">✗ Reject</button>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");
  }

  async function refreshSidebar() {
    try {
      const sims = await fetchJSON("/api/sims");
      renderShipments(sims);
    } catch {
      // ignore transient sidebar issues
    }
    try {
      const pending = await fetchJSON("/api/hitl/pending");
      renderHitl(pending);
    } catch {
      // ignore transient sidebar issues
    }
  }

  window.__focusSim = async (simId) => {
    if (!simId) return;
    activeSimId = simId;
    followActive = true;
    try {
      const state = await fetchJSON(`/api/sim/${activeSimId}`);
      if (state?.lat && state?.lon) map.panTo([state.lat, state.lon], { animate: true, duration: 0.5 });
    } catch {}
  };

  window.__hitlApprove = async (requestId) => {
    if (!requestId) return;
    try {
      await fetchJSON(`/api/hitl/${requestId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operator: "map_operator", notes: "Approved via map UI" }),
      });
      setStatus("Approved HITL request.");
      await refreshSidebar();
    } catch (e) {
      setStatus(`Approve failed: ${e.message}`);
    }
  };

  window.__hitlReject = async (requestId) => {
    if (!requestId) return;
    try {
      await fetchJSON(`/api/hitl/${requestId}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operator: "map_operator", notes: "Rejected via map UI" }),
      });
      setStatus("Rejected HITL request.");
      await refreshSidebar();
    } catch (e) {
      setStatus(`Reject failed: ${e.message}`);
    }
  };

  pollTimer = setInterval(async () => {
    if (!activeSimId) return;
    try {
      const state = await fetchJSON(`/api/sim/${activeSimId}`);
      const latlng = [state.lat, state.lon];
      shipmentMarker.setLatLng(latlng);
      setActiveInfo(state);
      if (followActive && state?.lat && state?.lon) {
        map.panTo([state.lat, state.lon], { animate: true, duration: 0.25 });
      }
      if (activeHintEl) {
        const top = state.anomalies && state.anomalies.length ? state.anomalies[0] : "—";
        activeHintEl.textContent = `${state.shipment_id} • ${state.phase} • ${state.risk_level || "—"} (${state.risk_score ?? "—"}) • ${String(top).replace(/_/g, " ")}`;
      }

      const color = riskColor(state.risk_level);
      shipmentMarker.setStyle({ color, fillColor: color });
      // Update weather zone visuals as weather changes over time
      if (originWeatherCircle && state.weather_origin) {
        originWeatherCircle.setStyle({
          color: state.weather_origin.color,
          fillColor: state.weather_origin.color,
        });
        originWeatherCircle.setRadius(
          Math.min(MAX_WEATHER_RADIUS_KM, state.weather_origin.radius_km || MAX_WEATHER_RADIUS_KM) * 1000
        );
        originWeatherCircle.bindTooltip(`Origin weather: ${state.weather_origin.label}`, { sticky: true });
      }
      if (destWeatherCircle && state.weather_destination) {
        destWeatherCircle.setStyle({
          color: state.weather_destination.color,
          fillColor: state.weather_destination.color,
        });
        destWeatherCircle.setRadius(
          Math.min(MAX_WEATHER_RADIUS_KM, state.weather_destination.radius_km || MAX_WEATHER_RADIUS_KM) * 1000
        );
        destWeatherCircle.bindTooltip(`Destination weather: ${state.weather_destination.label}`, { sticky: true });
      }

      if (state.phase === "WAIT_TAKEOFF") {
        setStatus(`Holding at origin (weather: ${state.weather_origin?.label || "unknown"})`);
      } else if (state.phase === "HOLDING") {
        setStatus(`Holding near destination (weather: ${state.weather_destination?.label || "unknown"})`);
      }
      if (state.phase === "ARRIVED") {
        setStatus(`Arrived: ${state.shipment_id} (${state.distance_km} km)`);
        stopPolling();
      }
    } catch (e) {
      setStatus(`Polling failed: ${e.message}`);
      stopPolling();
    }
  }, 650);

  // Sidebar refresh loop (per PDF: every ~3 seconds).
  sidebarTimer = setInterval(refreshSidebar, 3000);
  refreshSidebar();
}

async function applyWeatherOverrides() {
  if (!activeSimId) {
    setStatus("No active simulation to control.");
    return;
  }
  const origin_weather = document.getElementById("originWeather")?.value || undefined;
  const destination_weather = document.getElementById("destWeather")?.value || undefined;
  try {
    await fetchJSON(`/api/sim/${activeSimId}/weather`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin_weather, destination_weather }),
    });
    setStatus("Weather overrides applied.");
  } catch (e) {
    setStatus(`Weather override failed: ${e.message}`);
  }
}

function stopSim() {
  setStatus("Stopped.");
  stopPolling();
  clearRoute();
  setActiveInfo(null);
}

document.getElementById("startBtn").addEventListener("click", startSim);
document.getElementById("stopBtn").addEventListener("click", stopSim);
document.getElementById("resetBtn")?.addEventListener("click", async () => {
  try {
    const res = await fetchJSON("/api/demo/reset", { method: "POST" });
    stopSim();
    setStatus(`Reset done: cleared ${res.sims_cleared} sims, ${res.hitl_pending_cleared} HITL pending.`);
  } catch (e) {
    setStatus(`Reset failed: ${e.message}`);
  }
});
document.getElementById("clearAuditBtn")?.addEventListener("click", async () => {
  try {
    // Stop ongoing polling + in-memory sim activity first, otherwise the audit file
    // will immediately start filling again on the next tick.
    stopSim();
    try {
      await fetchJSON("/api/demo/reset", { method: "POST" });
    } catch {
      // ignore; audit clear can still run
    }
    const res = await fetchJSON("/api/audit/clear", { method: "POST" });
    const bytes = res?.audit?.bytes ?? "—";
    setStatus(`Audit log cleared (size now: ${bytes} bytes).`);
  } catch (e) {
    setStatus(`Clear audit failed: ${e.message}`);
  }
});
document.getElementById("originWeather")?.addEventListener("change", applyWeatherOverrides);
document.getElementById("destWeather")?.addEventListener("change", applyWeatherOverrides);

initMap();
loadOptions();

// Fast search-driven filtering (avoid rendering hundreds of <option> nodes).
let _searchTimer = null;
function onSearchChanged() {
  if (_searchTimer) clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => {
    refreshAirportSelects();
  }, 120);
}
originSearchEl?.addEventListener("input", onSearchChanged);
destSearchEl?.addEventListener("input", onSearchChanged);

// Auto-populate coordinates when a route endpoint changes.
// Also re-run lookup on click so selecting the same option again still resolves.
const originSelectEl = document.getElementById("originSelect");
const destSelectEl = document.getElementById("destSelect");
originSelectEl.addEventListener("change", () => lookupAndFill("origin"));
destSelectEl.addEventListener("change", () => lookupAndFill("dest"));
originSelectEl.addEventListener("click", () => setTimeout(() => lookupAndFill("origin"), 0));
destSelectEl.addEventListener("click", () => setTimeout(() => lookupAndFill("dest"), 0));

// Also redraw preview when user manually edits coordinates.
for (const id of ["originLat", "originLon", "destLat", "destLon"]) {
  document.getElementById(id).addEventListener("input", () => drawPreview());
}


