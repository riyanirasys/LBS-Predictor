from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path
import shutil

import folium
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from .config import Settings

logger = logging.getLogger(__name__)


def generate_clean_map(settings: Settings) -> str:
    """Generate a separate clean PS/FRV explorer map."""
    medoids = _load_json(settings.medoids_json)
    summaries = _load_json(settings.district_summaries_json)

    district_geojson, district_bounds = _load_district_boundaries(settings, summaries)
    ps_geojson, ps_bounds, district_ps_map, ps_lookup, ps_points = _load_ps_boundaries(settings)
    frv_points = _build_deployment_points(medoids, ps_lookup)
    _attach_frv_counts(ps_points, frv_points)

    center_lat, center_lon = _map_center(frv_points, settings)
    fmap = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=settings.map_zoom,
        tiles="cartodbpositron",
        control_scale=True,
        prefer_canvas=True,
    )

    _add_base_layers(fmap)
    _add_clean_controls(
        fmap=fmap,
        settings=settings,
        district_geojson=district_geojson,
        ps_geojson=ps_geojson,
        district_bounds=district_bounds,
        ps_bounds=ps_bounds,
        district_ps_map=district_ps_map,
        frv_points=frv_points,
        ps_points=ps_points,
    )

    all_bounds = _combined_bounds(district_bounds.values())
    if all_bounds:
        fmap.fit_bounds(all_bounds)

    output_path = _clean_output_path(settings.map_html)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fmap.save(output_path)

    assets_src = Path(__file__).parent.parent / "assets"
    assets_dst = output_path.parent / "assets"

    if assets_src.exists():

        if assets_dst.exists():
            shutil.rmtree(assets_dst)

        shutil.copytree(assets_src, assets_dst)

    logger.info("Clean map saved to %s", output_path)
    return str(output_path)


def generate_map(settings: Settings) -> str:
    """Compatibility wrapper if this file is used as the map module."""
    return generate_clean_map(settings)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _clean_output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_clean{path.suffix or '.html'}")


def _add_base_layers(fmap: folium.Map) -> None:
    folium.TileLayer("OpenStreetMap", name="Street Map").add_to(fmap)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="Satellite",
        overlay=False,
        control=True,
    ).add_to(fmap)


def _load_district_boundaries(settings: Settings, summaries: dict) -> tuple[dict, dict]:
    if not settings.districts_geojson.exists():
        return {"type": "FeatureCollection", "features": []}, {}

    districts = gpd.read_file(settings.districts_geojson).to_crs(epsg=4326)
    name_col = "dst_nme" if "dst_nme" in districts.columns else "dtname"
    districts["map_district"] = districts[name_col].astype(str).str.strip()
    districts["incidents"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("total_incidents", 0)
    )
    districts["frvs"] = districts["map_district"].apply(lambda name: summaries.get(name, {}).get("n_frvs", 0))
    districts["avg_resp"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("avg_response_time_min", 0)
    )
    bounds = {row.map_district: _geometry_bounds(row.geometry) for row in districts.itertuples()}
    geojson = json.loads(districts[["map_district", "incidents", "frvs", "avg_resp", "geometry"]].to_json())
    return geojson, bounds


def _load_ps_boundaries(settings: Settings) -> tuple[dict, dict, dict, list[dict], list[dict]]:
    if not settings.police_station_geojson.exists():
        return {"type": "FeatureCollection", "features": []}, {}, {}, [], []

    police_stations = gpd.read_file(settings.police_station_geojson).to_crs(epsg=4326)
    police_stations["map_district"] = police_stations["dst_nme"].astype(str).str.strip()
    police_stations["map_ps"] = police_stations["ps"].astype(str).str.strip()
    police_stations["map_ps_key"] = police_stations["map_district"] + "||" + police_stations["map_ps"]

    bounds: dict[str, list] = {}
    district_ps_map: dict[str, list] = {}
    ps_lookup: list[dict] = []
    ps_points: list[dict] = []
    for row in police_stations.itertuples():
        if not row.map_district or not row.map_ps:
            continue
        bounds[row.map_ps_key] = _geometry_bounds(row.geometry)
        district_ps_map.setdefault(row.map_district, []).append({"label": row.map_ps, "value": row.map_ps_key})
        point = row.geometry.representative_point()
        ps_lookup.append(
            {
                "district": row.map_district,
                "ps": row.map_ps,
                "psKey": row.map_ps_key,
                "geometry": row.geometry,
            }
        )
        ps_points.append(
            {
                "lat": float(point.y),
                "lon": float(point.x),
                "district": row.map_district,
                "ps": row.map_ps,
                "psKey": row.map_ps_key,
                "frvCount": 0,
                "nearestDistance": 0.0,
                "avgDistance": 0.0,
            }
        )

    for items in district_ps_map.values():
        items.sort(key=lambda item: item["label"])

    geojson = json.loads(police_stations[["map_district", "map_ps", "map_ps_key", "geometry"]].to_json())
    return geojson, bounds, district_ps_map, ps_lookup, ps_points


def _build_deployment_points(medoids: dict, ps_lookup: list[dict]) -> list[dict]:
    frv_points: list[dict] = []

    for info in medoids.values():
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            continue

        point = Point(float(lon), float(lat))
        matched_ps = _match_ps_for_point(point, str(info.get("district") or "").strip(), ps_lookup)
        district = matched_ps["district"] if matched_ps else str(info.get("district") or "").strip()
        ps_name = matched_ps["ps"] if matched_ps else _deployment_ps_name(info)
        ps_key = matched_ps["psKey"] if matched_ps else f"{district}||{ps_name}" if district and ps_name else ""
        avg_response = float(info.get("avg_response_time_min") or 0)

        frv_points.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "district": district,
                "ps": ps_name,
                "psKey": ps_key,
                "frvId": str(info.get("frv_id") or "N/A"),
                "avgResponse": avg_response,
                "maxResponse": float(info.get("max_response_time_min") or 0),
                "incidents": int(info.get("size") or 0),
                "nearestPs": str(info.get("nearest_police_station") or "N/A"),
                "popup": _medoid_popup(info),
            }
        )

    return frv_points


def _match_ps_for_point(point: Point, district: str, ps_lookup: list[dict]) -> dict | None:
    same_district = [item for item in ps_lookup if item["district"] == district]
    for item in same_district + [item for item in ps_lookup if item["district"] != district]:
        geometry = item["geometry"]
        if geometry.contains(point) or geometry.intersects(point):
            return item
    return None


def _attach_frv_counts(ps_points: list[dict], frv_points: list[dict]) -> None:
    counts: dict[str, int] = {}
    for point in frv_points:
        if point.get("psKey"):
            counts[point["psKey"]] = counts.get(point["psKey"], 0) + 1
    for point in ps_points:
        point["frvCount"] = counts.get(point["psKey"], 0)
        point["popup"] = _nearest_ps_popup(point)


def _deployment_ps_name(info: dict) -> str:
    for key in ("nearest_police_station", "police_station"):
        value = str(info.get(key) or "").strip()
        if value and value.upper() != "N/A":
            return value
    return ""


def _map_center(frv_points: list[dict], settings: Settings) -> tuple[float, float]:
    if not frv_points:
        return settings.map_center_lat, settings.map_center_lon
    return (
        sum(point["lat"] for point in frv_points) / len(frv_points),
        sum(point["lon"] for point in frv_points) / len(frv_points),
    )


def _add_clean_controls(
    fmap: folium.Map,
    settings: Settings,
    district_geojson: dict,
    ps_geojson: dict,
    district_bounds: dict,
    ps_bounds: dict,
    district_ps_map: dict,
    frv_points: list[dict],
    ps_points: list[dict],
) -> None:
    map_name = fmap.get_name()
    all_bounds = _combined_bounds(district_bounds.values())
    payload = {
        "districtGeojson": district_geojson,
        "psGeojson": ps_geojson,
        "districtBounds": district_bounds,
        "psBounds": ps_bounds,
        "districtPsMap": district_ps_map,
        "frvPoints": frv_points,
        "psPoints": ps_points,
        "allBounds": all_bounds,
        "defaultCenter": [settings.map_center_lat, settings.map_center_lon],
        "defaultZoom": settings.map_zoom,
    }

    html = f"""
    <style>
      :root {{
        --panel-bg: rgba(255,255,255,.96);
        --panel-border: rgba(20,36,52,.16);
        --ink: #17212b;
        --muted: #66717d;
        --blue: #1f6feb;
        --orange: #f97316;
        --green: #159947;
      }}
      .clean-map-ui {{
        position: fixed;
        top: 18px;
        left: 18px;
        z-index: 1200;
        font-family: "Segoe UI", Arial, sans-serif;
        color: var(--ink);
      }}
      .clean-map-toolbar {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }}
      .clean-map-button {{
        min-height: 38px;
        border: 1px solid var(--panel-border);
        border-radius: 6px;
        background: var(--panel-bg);
        color: var(--ink);
        padding: 0 12px;
        font-size: 13px;
        font-weight: 700;
        box-shadow: 0 8px 24px rgba(15, 23, 42, .16);
        cursor: pointer;
      }}
      .clean-map-button.active {{
        background: #17212b;
        color: #fff;
      }}
      .clean-map-panel {{
        display: none;
        width: min(360px, calc(100vw - 36px));
        margin-top: 8px;
        padding: 14px;
        border: 1px solid var(--panel-border);
        border-radius: 8px;
        background: var(--panel-bg);
        box-shadow: 0 18px 42px rgba(15, 23, 42, .22);
      }}
      .clean-map-panel.open {{ display: block; }}
      .clean-map-label {{
        display: block;
        margin: 10px 0 6px;
        font-size: 11px;
        font-weight: 700;
        color: var(--muted);
        text-transform: uppercase;
      }}
      .clean-map-select {{
        width: 100%;
        min-height: 38px;
        border: 1px solid rgba(23,33,43,.2);
        border-radius: 6px;
        background: #fff;
        color: var(--ink);
        padding: 0 10px;
        font-size: 13px;
      }}
      .clean-map-check {{
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 8px 0;
        font-size: 13px;
      }}
      .clean-map-note {{
        margin-top: 10px;
        color: var(--muted);
        font-size: 12px;
        line-height: 1.35;
      }}
      .legend-row {{
        display: grid;
        grid-template-columns: 24px 1fr;
        gap: 8px;
        align-items: center;
        margin: 9px 0;
        font-size: 13px;
      }}
      .legend-line {{
        width: 22px;
        height: 0;
        border-top: 3px solid var(--blue);
      }}
      .legend-line.ps {{
        border-top-color: var(--orange);
      }}
      .legend-line.selected {{
        border-top-color: #e11d48;
        border-top-width: 5px;
      }}
      .legend-dot {{
        width: 18px;
        height: 18px;
        border-radius: 50%;
        background: var(--green);
        border: 2px solid #fff;
        box-shadow: 0 0 0 2px rgba(21,153,71,.4);
      }}
      .legend-dot.frv-warn {{
        background: #dc2626;
        box-shadow: 0 0 0 2px rgba(220,38,38,.35);
      }}
      .legend-square {{
        width: 16px;
        height: 16px;
        border-radius: 3px;
        background: var(--blue);
      }}
      .clean-marker-label {{
        background: transparent;
        border: 0;
        color: #17212b;
        font-weight: 700;
        text-shadow: 0 1px 3px #fff;
      }}
      @media (max-width: 640px) {{
        .clean-map-ui {{
          left: 10px;
          right: 10px;
        }}
        .clean-map-panel {{
          width: calc(100vw - 20px);
        }}
      }}
    </style>

    <div class="clean-map-ui" id="clean-map-ui">
      <div class="clean-map-toolbar">
        <button class="clean-map-button" id="area-btn" type="button">Area Explorer</button>
        <button class="clean-map-button" id="filter-btn" type="button">Filters</button>
        <button class="clean-map-button" id="legend-btn" type="button">Notation</button>
      </div>

      <section class="clean-map-panel" id="area-panel">
        <label class="clean-map-label" for="district-select-clean">District</label>
        <select class="clean-map-select" id="district-select-clean"></select>
        <label class="clean-map-label" for="ps-select-clean">Police Station</label>
        <select class="clean-map-select" id="ps-select-clean" disabled></select>
        <div class="clean-map-note" id="selection-note">Select a district to show only that district's police stations and FRVs.</div>
      </section>

      <section class="clean-map-panel" id="filter-panel-clean">
        <label class="clean-map-check"><input id="toggle-districts" type="checkbox" checked> District boundary</label>
        <label class="clean-map-check"><input id="toggle-ps" type="checkbox" checked> Police-station boundaries</label>
        <label class="clean-map-check"><input id="toggle-ps-markers" type="checkbox" checked> Police-station markers</label>
        <label class="clean-map-check"><input id="toggle-frv" type="checkbox" checked> FRV markers</label>
      </section>

      <section class="clean-map-panel" id="legend-panel">
        <div class="legend-row"><span class="legend-line"></span><span>District boundary</span></div>
        <div class="legend-row"><span class="legend-line ps"></span><span>Police-station boundary</span></div>
        <div class="legend-row"><span class="legend-line selected"></span><span>Selected police-station boundary</span></div>
        <div class="legend-row"><span class="legend-square"></span><span>Police-station location</span></div>
        <div class="legend-row"><span class="legend-dot"></span><span>FRV with average response up to 10 minutes</span></div>
        <div class="legend-row"><span class="legend-dot frv-warn"></span><span>FRV with average response above 10 minutes</span></div>
      </section>
    </div>

    <script>
      const cleanMapName = "{map_name}";
      let cleanMap = null;
      const cleanPayload = {json.dumps(payload, ensure_ascii=False)};
      const cleanState = {{
        district: "",
        psKey: "",
        showDistricts: true,
        showPs: true,
        showPsMarkers: true,
        showFrv: true,
      }};
      const cleanLayers = {{
        districts: [],
        psBoundaries: [],
        psMarkers: [],
        frvMarkers: [],
      }};
      let cleanMapInitialized = false;

      function resolveCleanMap() {{
        if (cleanMap) return cleanMap;
        if (window[cleanMapName] && typeof window[cleanMapName].fitBounds === "function") {{
          cleanMap = window[cleanMapName];
          return cleanMap;
        }}
        for (const key in window) {{
          const candidate = window[key];
          if (
            candidate &&
            typeof candidate.fitBounds === "function" &&
            typeof candidate.setView === "function" &&
            typeof candidate.addLayer === "function"
          ) {{
            cleanMap = candidate;
            return cleanMap;
          }}
        }}
        return null;
      }}

      function cleanEscape(value) {{
        return String(value == null ? "" : value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;");
      }}

      function districtStyle(feature) {{
        const name = feature.properties.map_district;
        const active = cleanState.district && cleanState.district === name;
        return {{
          color: active ? "#1f6feb" : "#334155",
          weight: active ? 3 : 1.2,
          opacity: active ? 1 : .42,
          fillColor: "#60a5fa",
          fillOpacity: active ? .12 : .025,
        }};
      }}

      function psStyle(feature) {{
        const key = feature.properties.map_ps_key;
        const activeDistrict = cleanState.district && feature.properties.map_district === cleanState.district;
        const selected = cleanState.psKey && key === cleanState.psKey;
        return {{
          color: selected ? "#e11d48" : activeDistrict ? "#f97316" : "#94a3b8",
          weight: selected ? 4 : activeDistrict ? 2.1 : 1,
          opacity: selected || activeDistrict ? 1 : .32,
          fillColor: selected ? "#fb7185" : "#fdba74",
          fillOpacity: selected ? .20 : activeDistrict ? .10 : .02,
        }};
      }}

      function addGeoJsonLayers() {{
        L.geoJSON(cleanPayload.districtGeojson, {{
          style: districtStyle,
          onEachFeature: function(feature, layer) {{
            layer.cleanKind = "district";
            layer.cleanDistrict = feature.properties.map_district;
            layer.bindTooltip(feature.properties.map_district);
            layer.bindPopup(
              "<b>District:</b> " + cleanEscape(feature.properties.map_district) +
              "<br><b>Incidents:</b> " + cleanEscape(feature.properties.incidents) +
              "<br><b>FRVs:</b> " + cleanEscape(feature.properties.frvs) +
              "<br><b>Avg response:</b> " + cleanEscape(feature.properties.avg_resp)
            );
            cleanLayers.districts.push(layer);
          }}
        }}).addTo(cleanMap);

        L.geoJSON(cleanPayload.psGeojson, {{
          style: psStyle,
          onEachFeature: function(feature, layer) {{
            layer.cleanKind = "ps";
            layer.cleanDistrict = feature.properties.map_district;
            layer.cleanPsKey = feature.properties.map_ps_key;
            layer.bindTooltip(feature.properties.map_ps);
            layer.bindPopup(
              "<b>Police Station:</b> " + cleanEscape(feature.properties.map_ps) +
              "<br><b>District:</b> " + cleanEscape(feature.properties.map_district)
            );
            cleanLayers.psBoundaries.push(layer);
          }}
        }}).addTo(cleanMap);
      }}

      function makePsIcon() {{
          return L.icon({{
              iconUrl: 'assets/house.svg',
              iconSize: [28, 28],
              iconAnchor: [14, 14]
          }});
      }}

      function makeFrvIcon(point) {{

          const isGood = Number(point.avgResponse || 0) <= 10;

          # return L.icon({{
          #     iconUrl: isGood
          #         ? 'assets/car_green.svg'
          #         : 'assets/car_red.svg',

          #     iconSize: [32, 32],
          #     iconAnchor: [16, 16]
          # }});

        return L.divIcon({{
            html: `
                <svg>
                    <use href="assets/car.svg" fill="${{'#16a34a' if isGood else '#dc2626'}}" />
                </svg>
            `,
            className: '',
            iconSize: [28, 28],
            iconAnchor: [14, 14]
        }});
      }}

      function addPointLayers() {{
        cleanPayload.psPoints.forEach(function(point) {{
          const marker = L.marker([point.lat, point.lon], {{ icon: makePsIcon(), zIndexOffset: 500 }});
          marker.cleanDistrict = point.district;
          marker.cleanPsKey = point.psKey;
          marker.bindTooltip("PS: " + point.ps);
          marker.bindPopup(point.popup);
          marker.addTo(cleanMap);
          cleanLayers.psMarkers.push(marker);
        }});

        cleanPayload.frvPoints.forEach(function(point) {{
          const marker = L.marker([point.lat, point.lon], {{ icon: makeFrvIcon(point), zIndexOffset: 1000 }});
          marker.cleanDistrict = point.district;
          marker.cleanPsKey = point.psKey;
          marker.bindTooltip("FRV " + point.frvId);
          marker.bindPopup(point.popup);
          marker.addTo(cleanMap);
          cleanLayers.frvMarkers.push(marker);
        }});
      }}

      function layerAllowed(layer, kind) {{
        if (kind === "district") {{
          return cleanState.showDistricts && (!cleanState.district || layer.cleanDistrict === cleanState.district);
        }}
        if (kind === "ps") {{
          return cleanState.showPs && (!cleanState.district || layer.cleanDistrict === cleanState.district);
        }}
        if (kind === "psMarker") {{
          return cleanState.showPsMarkers && (!cleanState.district || layer.cleanDistrict === cleanState.district);
        }}
        if (kind === "frv") {{
          return cleanState.showFrv &&
            (!cleanState.district || layer.cleanDistrict === cleanState.district) &&
            (!cleanState.psKey || layer.cleanPsKey === cleanState.psKey);
        }}
        return true;
      }}

      function setLayerPresence(layer, shouldShow) {{
        const visible = cleanMap.hasLayer(layer);
        if (shouldShow && !visible) cleanMap.addLayer(layer);
        if (!shouldShow && visible) cleanMap.removeLayer(layer);
      }}

      function applyCleanFilters() {{
        cleanLayers.districts.forEach(function(layer) {{
          if (layer.setStyle) layer.setStyle(districtStyle(layer.feature));
          setLayerPresence(layer, layerAllowed(layer, "district"));
        }});
        cleanLayers.psBoundaries.forEach(function(layer) {{
          if (layer.setStyle) layer.setStyle(psStyle(layer.feature));
          setLayerPresence(layer, layerAllowed(layer, "ps"));
        }});
        cleanLayers.psMarkers.forEach(function(layer) {{
          setLayerPresence(layer, layerAllowed(layer, "psMarker"));
        }});
        cleanLayers.frvMarkers.forEach(function(layer) {{
          setLayerPresence(layer, layerAllowed(layer, "frv"));
        }});
      }}

      function populateDistricts() {{
        const districtSelect = document.getElementById("district-select-clean");
        districtSelect.innerHTML = '<option value="">View all MP</option>';
        Object.keys(cleanPayload.districtPsMap).sort().forEach(function(district) {{
          const option = document.createElement("option");
          option.value = district;
          option.textContent = district;
          districtSelect.appendChild(option);
        }});
      }}

      function populatePoliceStations(district) {{
        const psSelect = document.getElementById("ps-select-clean");
        psSelect.innerHTML = "";
        if (!district) {{
          psSelect.disabled = true;
          psSelect.innerHTML = '<option value="">Select district first</option>';
          return;
        }}
        psSelect.disabled = false;
        const allOption = document.createElement("option");
        allOption.value = "";
        allOption.textContent = "All police stations in " + district;
        psSelect.appendChild(allOption);
        (cleanPayload.districtPsMap[district] || []).forEach(function(item) {{
          const option = document.createElement("option");
          option.value = item.value;
          option.textContent = item.label;
          psSelect.appendChild(option);
        }});
      }}

      function fitCleanSelection() {{
        if (cleanState.psKey && cleanPayload.psBounds[cleanState.psKey]) {{
          cleanMap.fitBounds(cleanPayload.psBounds[cleanState.psKey], {{ padding: [24, 24] }});
          return;
        }}
        if (cleanState.district && cleanPayload.districtBounds[cleanState.district]) {{
          cleanMap.fitBounds(cleanPayload.districtBounds[cleanState.district], {{ padding: [24, 24] }});
          return;
        }}
        if (cleanPayload.allBounds) {{
          cleanMap.fitBounds(cleanPayload.allBounds, {{ padding: [18, 18] }});
        }} else {{
          cleanMap.setView(cleanPayload.defaultCenter, cleanPayload.defaultZoom);
        }}
      }}

      function updateSelectionNote() {{
        const note = document.getElementById("selection-note");
        const frvCount = cleanLayers.frvMarkers.filter(function(layer) {{
          return cleanMap.hasLayer(layer);
        }}).length;
        const psCount = cleanLayers.psMarkers.filter(function(layer) {{
          return cleanMap.hasLayer(layer);
        }}).length;
        if (cleanState.psKey) {{
          const label = document.getElementById("ps-select-clean").selectedOptions[0].textContent;
          note.textContent = "Showing " + frvCount + " FRV marker(s) for " + label + ".";
        }} else if (cleanState.district) {{
          note.textContent = "Showing " + psCount + " police station marker(s) and " + frvCount + " FRV marker(s) only for " + cleanState.district + ".";
        }} else {{
          note.textContent = "Select a district to show only that district's police stations and FRVs.";
        }}
      }}

      function openPanel(panelId, buttonId) {{
        const panel = document.getElementById(panelId);
        const shouldOpen = !panel.classList.contains("open");
        ["area-panel", "filter-panel-clean", "legend-panel"].forEach(function(id) {{
          document.getElementById(id).classList.toggle("open", id === panelId && shouldOpen);
        }});
        ["area-btn", "filter-btn", "legend-btn"].forEach(function(id) {{
          document.getElementById(id).classList.remove("active");
        }});
        document.getElementById(buttonId).classList.toggle("active", shouldOpen);
      }}

      function wireControls() {{
        const ui = document.getElementById("clean-map-ui");
        if (L.DomEvent) {{
          L.DomEvent.disableClickPropagation(ui);
          L.DomEvent.disableScrollPropagation(ui);
        }}

        document.getElementById("area-btn").onclick = function() {{ openPanel("area-panel", "area-btn"); }};
        document.getElementById("filter-btn").onclick = function() {{ openPanel("filter-panel-clean", "filter-btn"); }};
        document.getElementById("legend-btn").onclick = function() {{ openPanel("legend-panel", "legend-btn"); }};

        document.getElementById("district-select-clean").onchange = function(event) {{
          cleanState.district = event.target.value;
          cleanState.psKey = "";
          populatePoliceStations(cleanState.district);
          applyCleanFilters();
          fitCleanSelection();
          updateSelectionNote();
        }};

        document.getElementById("ps-select-clean").onchange = function(event) {{
          cleanState.psKey = event.target.value;
          applyCleanFilters();
          fitCleanSelection();
          updateSelectionNote();
        }};

        [
          ["toggle-districts", "showDistricts"],
          ["toggle-ps", "showPs"],
          ["toggle-ps-markers", "showPsMarkers"],
          ["toggle-frv", "showFrv"],
        ].forEach(function(pair) {{
          document.getElementById(pair[0]).onchange = function(event) {{
            cleanState[pair[1]] = event.target.checked;
            applyCleanFilters();
          }};
        }});
      }}

      function initCleanMap() {{
        if (cleanMapInitialized) return;
        if (!window.L || !resolveCleanMap()) {{
          window.setTimeout(initCleanMap, 80);
          return;
        }}
        cleanMapInitialized = true;
        addGeoJsonLayers();
        addPointLayers();
        populateDistricts();
        populatePoliceStations("");
        wireControls();
        applyCleanFilters();
      }}

      if (document.readyState === "loading") {{
        document.addEventListener("DOMContentLoaded", initCleanMap);
      }} else {{
        initCleanMap();
      }}
      window.addEventListener("load", initCleanMap);
    </script>
    """
    fmap.get_root().html.add_child(folium.Element(html))


def _geometry_bounds(geometry) -> list:
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    return [[min_lat, min_lon], [max_lat, max_lon]]


def _combined_bounds(bounds_list) -> list | None:
    bounds = list(bounds_list)
    if not bounds:
        return None
    min_lat = min(item[0][0] for item in bounds)
    min_lon = min(item[0][1] for item in bounds)
    max_lat = max(item[1][0] for item in bounds)
    max_lon = max(item[1][1] for item in bounds)
    return [[min_lat, min_lon], [max_lat, max_lon]]


def _medoid_popup(info: dict) -> str:
    avg_resp = float(info.get("avg_response_time_min") or 0)
    max_resp = float(info.get("max_response_time_min") or 0)
    resp_color = "#159947" if avg_resp <= 10 else "#dc2626"
    nearest_ps = escape(str(info.get("nearest_police_station") or "N/A"))
    nearest_distance = info.get("road_factor_distance_to_nearest_ps_km")
    if nearest_distance is None and info.get("distance_to_nearest_ps_km") is not None:
        nearest_distance = info.get("distance_to_nearest_ps_km")
    nearest_label = nearest_ps if nearest_distance is None else f"{nearest_ps} ({float(nearest_distance):.2f} km)"
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:285px;color:#17212b">
      <h3 style="margin:0 0 8px 0;font-size:17px;color:{resp_color}">FRV {escape(str(info.get('frv_id', 'N/A')))}</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr><td style="padding:4px 0;color:#66717d"><b>District</b></td><td style="text-align:right">{escape(str(info.get('district', 'N/A')))}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Police Station</b></td><td style="text-align:right">{escape(_deployment_ps_name(info) or 'N/A')}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Nearest PS</b></td><td style="text-align:right">{nearest_label}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Incidents</b></td><td style="text-align:right">{int(info.get('size') or 0):,}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Max response</b></td><td style="text-align:right">{_format_minutes(max_resp)}</td></tr>
      </table>
      <div style="margin-top:10px;padding:8px;border-radius:6px;background:{resp_color};color:#fff;text-align:center;font-weight:700">
        Avg response: {_format_minutes(avg_resp)}
      </div>
    </div>
    """


def _nearest_ps_popup(info: dict) -> str:
    return f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;min-width:260px;color:#17212b">
      <h3 style="margin:0 0 8px 0;font-size:17px;color:#1f6feb">Police Station</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <tr><td style="padding:4px 0;color:#66717d"><b>Name</b></td><td style="text-align:right">{escape(str(info.get('ps') or 'N/A'))}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>District</b></td><td style="text-align:right">{escape(str(info.get('district') or 'N/A'))}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>FRVs in PS</b></td><td style="text-align:right">{int(info.get('frvCount') or 0)}</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Nearest FRV distance</b></td><td style="text-align:right">{float(info.get('nearestDistance') or 0):.2f} km</td></tr>
        <tr><td style="padding:4px 0;color:#66717d"><b>Avg FRV distance</b></td><td style="text-align:right">{float(info.get('avgDistance') or 0):.2f} km</td></tr>
      </table>
    </div>
    """


def _format_minutes(value: object) -> str:
    try:
        total_seconds = int(round(float(value) * 60))
    except Exception:
        total_seconds = 0
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"
