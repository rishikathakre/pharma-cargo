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

const statusEl = document.getElementById("status");
const activeInfoEl = document.getElementById("activeInfo");

function setStatus(msg) {
  statusEl.textContent = msg;
}

function setActiveInfo(obj) {
  activeInfoEl.textContent = obj ? JSON.stringify(obj, null, 2) : "None";
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

function fillSelect(selectEl, values) {
  selectEl.innerHTML = "";
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    selectEl.appendChild(opt);
  }
}

async function loadOptions() {
  const opts = await fetchJSON("/api/options");
  fillSelect(document.getElementById("originSelect"), opts.origins);
  fillSelect(document.getElementById("destSelect"), opts.destinations);
  setStatus(
    `Loaded ${opts.routes_count} routes from ${opts.source_file} (airports in dropdown: ${opts.airports_indexed})`
  );
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
    previewLine = L.polyline(
      [
        [oLat, oLon],
        [dLat, dLon],
      ],
      { color: "#94a3b8", weight: 3, opacity: 0.8, dashArray: "6 8" }
    ).addTo(map);
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
    radius: (oW.radius_km || 60) * 1000,
    color: oW.color || "#64748b",
    fillColor: oW.color || "#64748b",
    fillOpacity: 0.15,
    weight: 2,
  }).addTo(map);
  originWeatherCircle.bindTooltip(`Origin weather: ${oW.label}`, { sticky: true });

  destWeatherCircle = L.circle(destLatLng, {
    radius: (dW.radius_km || 60) * 1000,
    color: dW.color || "#64748b",
    fillColor: dW.color || "#64748b",
    fillOpacity: 0.15,
    weight: 2,
  }).addTo(map);
  destWeatherCircle.bindTooltip(`Destination weather: ${dW.label}`, { sticky: true });

  routeLine = L.polyline([originLatLng, destLatLng], {
    color: "#2563eb",
    weight: 4,
    opacity: 0.85,
  }).addTo(map);

  shipmentMarker = L.circleMarker(originLatLng, {
    radius: 8,
    color: "#b91c1c",
    fillColor: "#ef4444",
    fillOpacity: 0.9,
    weight: 2,
  }).addTo(map);

  shipmentMarker.bindPopup("Click to show route").openPopup();
  shipmentMarker.on("click", () => {
    if (routeLine) routeLine.setStyle({ color: "#dc2626" });
  });

  map.fitBounds(routeLine.getBounds(), { padding: [40, 40] });

  pollTimer = setInterval(async () => {
    if (!activeSimId) return;
    try {
      const state = await fetchJSON(`/api/sim/${activeSimId}`);
      const latlng = [state.lat, state.lon];
      shipmentMarker.setLatLng(latlng);
      setActiveInfo(state);
      // Update weather zone visuals as weather changes over time
      if (originWeatherCircle && state.weather_origin) {
        originWeatherCircle.setStyle({
          color: state.weather_origin.color,
          fillColor: state.weather_origin.color,
        });
        originWeatherCircle.setRadius((state.weather_origin.radius_km || 60) * 1000);
        originWeatherCircle.bindTooltip(`Origin weather: ${state.weather_origin.label}`, { sticky: true });
      }
      if (destWeatherCircle && state.weather_destination) {
        destWeatherCircle.setStyle({
          color: state.weather_destination.color,
          fillColor: state.weather_destination.color,
        });
        destWeatherCircle.setRadius((state.weather_destination.radius_km || 60) * 1000);
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
  }, 300);
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
document.getElementById("originWeather")?.addEventListener("change", applyWeatherOverrides);
document.getElementById("destWeather")?.addEventListener("change", applyWeatherOverrides);

initMap();
loadOptions();

// Auto-populate coordinates when a route endpoint changes.
document.getElementById("originSelect").addEventListener("change", () => lookupAndFill("origin"));
document.getElementById("destSelect").addEventListener("change", () => lookupAndFill("dest"));

// Also redraw preview when user manually edits coordinates.
for (const id of ["originLat", "originLon", "destLat", "destLon"]) {
  document.getElementById(id).addEventListener("input", () => drawPreview());
}


