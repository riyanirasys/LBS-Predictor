/* =========================================================
   LBS Predictor – Clean Map  |  map.js
   ========================================================= */

"use strict";

// ── State ─────────────────────────────────────────────────
const state = {
  district:      "",
  psKey:         "",
  showDistricts: true,
  showPs:        true,
  showPsMarkers: true,
  showFrv:       true,
};

const layers = {
  districts:    [],
  psBoundaries: [],
  psMarkers:    [],
  frvMarkers:   [],
};

let map     = null;
let payload = null;

// ── Bootstrap ─────────────────────────────────────────────
async function init() {
  try {
    const res = await fetch("map_data.json");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (err) {
    showError("Could not load map_data.json: " + err.message);
    return;
  }

  map = L.map("map", { preferCanvas: true, zoomControl: true });

  // Base tile layers
  const carto = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    { attribution: "© CartoDB", subdomains: "abcd", maxZoom: 19 }
  ).addTo(map);

  const osm = L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "© OpenStreetMap contributors", maxZoom: 19 }
  );

  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { attribution: "© Esri", maxZoom: 19 }
  );

  L.control.layers(
    { "Light (default)": carto, "Street Map": osm, "Satellite": satellite },
    {},
    { position: "topright" }
  ).addTo(map);
  L.control.scale().addTo(map);

  // Set initial view
  if (payload.allBounds) {
    map.fitBounds(payload.allBounds, { padding: [18, 18] });
  } else {
    map.setView(payload.defaultCenter, payload.defaultZoom);
  }

  addGeoJsonLayers();
  addPointLayers();
  populateDistricts();
  populatePoliceStations("");
  wireControls();
  applyFilters();

  hideLoading();
}

// ── GeoJSON layers ────────────────────────────────────────
function addGeoJsonLayers() {
  L.geoJSON(payload.districtGeojson, {
    style: districtStyle,
    onEachFeature(feature, layer) {
      layer._kind     = "district";
      layer._district = feature.properties.map_district;
      layer.bindTooltip(feature.properties.map_district);
      layer.bindPopup(
        `<b>District:</b> ${esc(feature.properties.map_district)}<br>` +
        `<b>Incidents:</b> ${esc(feature.properties.incidents)}<br>` +
        `<b>FRVs:</b> ${esc(feature.properties.frvs)}<br>` +
        `<b>Avg response:</b> ${esc(feature.properties.avg_resp)}`
      );
      layers.districts.push(layer);
    },
  }).addTo(map);

  L.geoJSON(payload.psGeojson, {
    style: psStyle,
    onEachFeature(feature, layer) {
      layer._kind     = "ps";
      layer._district = feature.properties.map_district;
      layer._psKey    = feature.properties.map_ps_key;
      layer.bindTooltip(feature.properties.map_ps);
      layer.bindPopup(
        `<b>Police Station:</b> ${esc(feature.properties.map_ps)}<br>` +
        `<b>District:</b> ${esc(feature.properties.map_district)}`
      );
      layers.psBoundaries.push(layer);
    },
  }).addTo(map);
}

// ── Point layers ──────────────────────────────────────────
function addPointLayers() {
  payload.psPoints.forEach(pt => {
    const marker = L.marker([pt.lat, pt.lon], { icon: makePsIcon(), zIndexOffset: 500 });
    marker._district = pt.district;
    marker._psKey    = pt.psKey;
    marker.bindTooltip("PS: " + pt.ps);
    marker.bindPopup(buildPsPopup(pt));
    marker.addTo(map);
    layers.psMarkers.push(marker);
  });

  payload.frvPoints.forEach(pt => {
    const marker = L.marker([pt.lat, pt.lon], { icon: makeFrvIcon(pt), zIndexOffset: 1000 });
    marker._district = pt.district;
    marker._psKey    = pt.psKey;
    marker.bindTooltip("FRV " + pt.frvId);
    marker.bindPopup(buildFrvPopup(pt));
    marker.addTo(map);
    layers.frvMarkers.push(marker);
  });
}

// ── Icons ─────────────────────────────────────────────────
function makePsIcon() {
  const color   = "#00aeff" ;

  return  L.divIcon({
    html: `<svg fill="${color}"> <use href="assets/police-station.svg"></use> </svg>`,
    className:  "",
    iconSize:  [28, 28],
    iconAnchor:[14, 14],
  });
}

function makeFrvIcon(pt) {
  const isGood  = Number(pt.avgResponse || 0) <= 10;
  const color   = isGood ? "#16a34a" : "#dc2626";
  const shadow  = isGood ? "rgba(22,163,74,.35)" : "rgba(220,38,38,.35)";

  // Inline SVG car icon — swap for L.icon(…) once you place assets/car_green.svg etc.
  return L.divIcon({
    html: `<svg fill="${color}"> <use href="assets/police-car.svg"></use> </svg>`,
    className:  "",
    iconSize:   [28, 28],
    iconAnchor: [14, 14],
  });
}

// ── Popup builders ────────────────────────────────────────
function buildFrvPopup(pt) {
  const avg       = Number(pt.avgResponse || 0);
  const max       = Number(pt.maxResponse || 0);
  const color     = avg <= 10 ? "#159947" : "#dc2626";
  const nearestLbl = pt.nearestDistance != null
    ? `${esc(pt.nearestPs)} (${Number(pt.nearestDistance).toFixed(2)} km)`
    : esc(pt.nearestPs || "N/A");

  return `
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:285px;color:#17212b">
      <h3 style="margin:0 0 8px 0;font-size:17px;color:${color}">FRV ${esc(pt.frvId)}</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>
            <td style="text-align:right">${esc(pt.district)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Police Station</b></td>
            <td style="text-align:right">${esc(pt.ps || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Nearest PS</b></td>
            <td style="text-align:right">${nearestLbl}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Incidents</b></td>
            <td style="text-align:right">${Number(pt.incidents || 0).toLocaleString()}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Max response</b></td>
            <td style="text-align:right">${formatMinutes(max)}</td></tr>
      </table>
      <div style="margin-top:10px;padding:8px;border-radius:6px;background:${color};
                  color:#fff;text-align:center;font-weight:700">
        Avg response: ${formatMinutes(avg)}
      </div>
    </div>`;
}

function buildPsPopup(pt) {
  return `
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:260px;color:#17212b">
      <h3 style="margin:0 0 8px 0;font-size:17px;color:#1f6feb">Police Station</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr><td style="padding:4px 0;color:#66717d"><b>Name</b></td>
            <td style="text-align:right">${esc(pt.ps || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>
            <td style="text-align:right">${esc(pt.district || "N/A")}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>FRVs in PS</b></td>
            <td style="text-align:right">${Number(pt.frvCount || 0)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Nearest FRV distance</b></td>
            <td style="text-align:right">${Number(pt.nearestDistance || 0).toFixed(2)} km</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Avg FRV distance</b></td>
            <td style="text-align:right">${Number(pt.avgDistance || 0).toFixed(2)} km</td></tr>
      </table>
    </div>`;
}

// ── Styles ────────────────────────────────────────────────
function districtStyle(feature) {
  const name   = feature.properties.map_district;
  const active = state.district && state.district === name;
  return {
    color:       active ? "#1f6feb" : "#334155",
    weight:      active ? 3 : 1.2,
    opacity:     active ? 1 : 0.42,
    fillColor:   "#60a5fa",
    fillOpacity: active ? 0.12 : 0.025,
  };
}

function psStyle(feature) {
  const key           = feature.properties.map_ps_key;
  const activeDistrict = state.district && feature.properties.map_district === state.district;
  const selected       = state.psKey && key === state.psKey;
  return {
    color:       selected ? "#e11d48" : activeDistrict ? "#f97316" : "#94a3b8",
    weight:      selected ? 4 : activeDistrict ? 2.1 : 1,
    opacity:     selected || activeDistrict ? 1 : 0.32,
    fillColor:   selected ? "#fb7185" : "#fdba74",
    fillOpacity: selected ? 0.20 : activeDistrict ? 0.10 : 0.02,
  };
}

// ── Filter logic ──────────────────────────────────────────
function layerVisible(layer, kind) {
  if (kind === "district")  return state.showDistricts && (!state.district || layer._district === state.district);
  if (kind === "ps")        return state.showPs        && (!state.district || layer._district === state.district);
  if (kind === "psMarker")  return state.showPsMarkers && (!state.district || layer._district === state.district);
  if (kind === "frv") {
    return state.showFrv
      && (!state.district || layer._district === state.district)
      && (!state.psKey    || layer._psKey    === state.psKey);
  }
  return true;
}

function setPresence(layer, show) {
  const has = map.hasLayer(layer);
  if (show && !has) map.addLayer(layer);
  if (!show && has) map.removeLayer(layer);
}

function applyFilters() {
  layers.districts.forEach(l => {
    if (l.setStyle) l.setStyle(districtStyle(l.feature));
    setPresence(l, layerVisible(l, "district"));
  });
  layers.psBoundaries.forEach(l => {
    if (l.setStyle) l.setStyle(psStyle(l.feature));
    setPresence(l, layerVisible(l, "ps"));
  });
  layers.psMarkers.forEach(l => setPresence(l, layerVisible(l, "psMarker")));
  layers.frvMarkers.forEach(l => setPresence(l, layerVisible(l, "frv")));
  updateNote();
}

// ── Fit helpers ───────────────────────────────────────────
function fitSelection() {
  if (state.psKey && payload.psBounds[state.psKey]) {
    map.fitBounds(payload.psBounds[state.psKey], { padding: [24, 24] });
    return;
  }
  if (state.district && payload.districtBounds[state.district]) {
    map.fitBounds(payload.districtBounds[state.district], { padding: [24, 24] });
    return;
  }
  if (payload.allBounds) {
    map.fitBounds(payload.allBounds, { padding: [18, 18] });
  } else {
    map.setView(payload.defaultCenter, payload.defaultZoom);
  }
}

// ── Dropdowns ─────────────────────────────────────────────
function populateDistricts() {
  const sel = document.getElementById("district-select");
  sel.innerHTML = '<option value="">View all</option>';
  Object.keys(payload.districtPsMap).sort().forEach(d => {
    const opt = document.createElement("option");
    opt.value       = d;
    opt.textContent = d;
    sel.appendChild(opt);
  });
}

function populatePoliceStations(district) {
  const sel = document.getElementById("ps-select");
  sel.innerHTML = "";
  if (!district) {
    sel.disabled     = true;
    sel.innerHTML    = '<option value="">Select district first</option>';
    return;
  }
  sel.disabled = false;
  const allOpt = document.createElement("option");
  allOpt.value       = "";
  allOpt.textContent = `All police stations in ${district}`;
  sel.appendChild(allOpt);
  (payload.districtPsMap[district] || []).forEach(item => {
    const opt = document.createElement("option");
    opt.value       = item.value;
    opt.textContent = item.label;
    sel.appendChild(opt);
  });
}

// ── Note text ─────────────────────────────────────────────
function updateNote() {
  const note     = document.getElementById("selection-note");
  const frvCount = layers.frvMarkers.filter(l => map.hasLayer(l)).length;
  const psCount  = layers.psMarkers.filter(l => map.hasLayer(l)).length;

  if (state.psKey) {
    const label = document.getElementById("ps-select").selectedOptions[0]?.textContent || "";
    note.textContent = `Showing ${frvCount} FRV marker(s) for ${label}.`;
  } else if (state.district) {
    note.textContent = `Showing ${psCount} police station(s) and ${frvCount} FRV(s) in ${state.district}.`;
  } else {
    note.textContent = "Select a district to filter police stations and FRVs.";
  }
}

// ── Panel toggle ──────────────────────────────────────────
function openPanel(panelId, btnId) {
  const panel     = document.getElementById(panelId);
  const shouldOpen = !panel.classList.contains("open");
  ["area-panel", "filter-panel", "legend-panel"].forEach(id =>
    document.getElementById(id).classList.toggle("open", id === panelId && shouldOpen)
  );
  ["area-btn", "filter-btn", "legend-btn"].forEach(id =>
    document.getElementById(id).classList.remove("active")
  );
  document.getElementById(btnId).classList.toggle("active", shouldOpen);
}

// ── Wire up UI ────────────────────────────────────────────
function wireControls() {
  const ui = document.getElementById("map-ui");
  if (L.DomEvent) {
    L.DomEvent.disableClickPropagation(ui);
    L.DomEvent.disableScrollPropagation(ui);
  }

  document.getElementById("area-btn").onclick   = () => openPanel("area-panel",   "area-btn");
  document.getElementById("filter-btn").onclick = () => openPanel("filter-panel", "filter-btn");
  document.getElementById("legend-btn").onclick = () => openPanel("legend-panel", "legend-btn");

  document.getElementById("district-select").onchange = e => {
    state.district = e.target.value;
    state.psKey    = "";
    populatePoliceStations(state.district);
    applyFilters();
    fitSelection();
  };

  document.getElementById("ps-select").onchange = e => {
    state.psKey = e.target.value;
    applyFilters();
    fitSelection();
  };

  [
    ["toggle-districts", "showDistricts"],
    ["toggle-ps",        "showPs"],
    ["toggle-ps-markers","showPsMarkers"],
    ["toggle-frv",       "showFrv"],
  ].forEach(([id, key]) => {
    document.getElementById(id).onchange = e => {
      state[key] = e.target.checked;
      applyFilters();
    };
  });
}

// ── Utilities ─────────────────────────────────────────────
function esc(v) {
  return String(v == null ? "" : v)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatMinutes(value) {
  const total   = Math.round(Number(value) * 60) || 0;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

function hideLoading() {
  document.getElementById("loading-overlay").classList.add("hidden");
}

function showError(msg) {
  const el = document.getElementById("loading-overlay");
  el.innerHTML = `<div style="color:#dc2626;font-size:15px;max-width:420px;text-align:center">
    <b>Error loading map data</b><br><br>${esc(msg)}<br><br>
    Make sure <code>map_data.json</code> is in the same folder as this page,
    and that you're serving it via a local HTTP server (not <code>file://</code>).
  </div>`;
}

// ── Start ─────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", init);