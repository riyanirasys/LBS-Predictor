from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from .config import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers to clean NaN/Inf for JSON
# ---------------------------------------------------------------------------

def _sanitize_val(val):
    import math
    if isinstance(val, dict):
        return {k: _sanitize_val(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [_sanitize_val(v) for v in val]
    elif isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    elif hasattr(val, "item"):
        return _sanitize_val(val.item())
    return val


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def generate_clean_map(settings: Settings) -> str:
    """Build map_data.json consumed by the standalone web/ map."""
    medoids   = _load_json(settings.medoids_json)
    summaries = _load_json(settings.district_summaries_json)

    # Check for sufficiency data
    ps_suff_path = settings.output_dir / "ps_sufficiency_summary.csv"
    dist_suff_path = settings.output_dir / "district_sufficiency_summary.csv"
    placements_path = settings.output_dir / "sufficiency_placements.csv"

    ps_suff_df = pd.read_csv(ps_suff_path) if ps_suff_path.exists() else None
    dist_suff_df = pd.read_csv(dist_suff_path) if dist_suff_path.exists() else None
    placements_df = pd.read_csv(placements_path) if placements_path.exists() else None

    district_geojson, district_bounds = _load_district_boundaries(settings, summaries, dist_suff_df)
    ps_geojson, ps_bounds, district_ps_map, ps_lookup, ps_points = _load_ps_boundaries(settings, ps_suff_df)
    
    # Build scenarios
    frv_scenarios = {}
    if placements_df is not None:
        for target, group in placements_df.groupby("target"):
            points = []
            for idx, row in enumerate(group.itertuples(), 1):
                ps_name = str(row.police_station)
                district = str(row.district)
                ps_key = f"{district}||{ps_name}"
                
                # Check for nearest PS distance
                # In sufficiency, placements are grid cells which belong to a PS.
                # So distance to nearest PS is typically 0.0 or the distance to the PS center.
                avg_resp = float(row.avg_response) if pd.notna(getattr(row, "avg_response", None)) else 0.0
                max_resp = float(row.max_response) if pd.notna(getattr(row, "max_response", None)) else 0.0
                inc = int(row.incidents) if pd.notna(getattr(row, "incidents", None)) else 0

                points.append({
                    "lat": float(row.latitude),
                    "lon": float(row.longitude),
                    "district": district,
                    "ps": ps_name,
                    "psKey": ps_key,
                    "frvId": f"REQ-{target.upper()}-{row.placement_id}",
                    "avgResponse": avg_resp,
                    "maxResponse": max_resp,
                    "incidents": inc,
                    "nearestPs": ps_name,
                    "nearestDistance": 0.0,
                })
            frv_scenarios[str(target)] = points
            
    # Default frvPoints to current scenario if available, else fallback
    if "current" in frv_scenarios:
        frv_points = frv_scenarios["current"]
    else:
        frv_points = _build_deployment_points(medoids, ps_lookup)
        frv_scenarios["current"] = frv_points

    _attach_frv_counts(ps_points, frv_points)

    center_lat, center_lon = _map_center(frv_points, settings)
    all_bounds = _combined_bounds(district_bounds.values())

    payload = {
        "districtGeojson": district_geojson,
        "psGeojson":        ps_geojson,
        "districtBounds":   district_bounds,
        "psBounds":         ps_bounds,
        "districtPsMap":    district_ps_map,
        "frvPoints":        frv_points,
        "frvScenarios":     frv_scenarios,
        "psPoints":         ps_points,
        "allBounds":        all_bounds,
        "defaultCenter":    [center_lat, center_lon],
        "defaultZoom":      settings.map_zoom,
        "hasSufficiency":   placements_df is not None,
    }

    output_path = _data_output_path(settings.map_html)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sanitized_payload = _sanitize_val(payload)
    output_path.write_text(json.dumps(sanitized_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Map data saved to %s", output_path)
    return str(output_path)


def generate_map(settings: Settings) -> str:
    """Compatibility wrapper."""
    return generate_clean_map(settings)


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_district_boundaries(settings: Settings, summaries: dict, dist_suff_df: pd.DataFrame | None = None) -> tuple[dict, dict]:
    if not settings.districts_geojson.exists():
        return {"type": "FeatureCollection", "features": []}, {}

    districts = gpd.read_file(settings.districts_geojson).to_crs(epsg=4326)
    name_col = "dst_nme" if "dst_nme" in districts.columns else "dtname"
    districts["map_district"] = districts[name_col].astype(str).str.strip()
    districts["incidents"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("total_incidents", 0)
    )
    districts["frvs"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("n_frvs", 0)
    )
    districts["avg_resp"] = districts["map_district"].apply(
        lambda name: summaries.get(name, {}).get("avg_response_time_min", 0)
    )
    
    # Load sufficiency summary statistics
    for col in ["current_frvs", "current_rt_min", "req_frvs_10m", "req_rt_10m", "add_frvs_10m", "req_frvs_5m", "req_rt_5m", "add_frvs_5m"]:
        if dist_suff_df is not None and col in dist_suff_df.columns:
            lookup = dict(zip(dist_suff_df["district"], dist_suff_df[col]))
            # Handle possible float/int formats and NaN mapping
            districts[col] = districts["map_district"].map(lookup).fillna(0)
        else:
            districts[col] = 0

    bounds  = {row.map_district: _geometry_bounds(row.geometry) for row in districts.itertuples()}
    
    geojson_cols = [
        "map_district", "incidents", "frvs", "avg_resp", "geometry",
        "current_frvs", "current_rt_min", "req_frvs_10m", "req_rt_10m",
        "add_frvs_10m", "req_frvs_5m", "req_rt_5m", "add_frvs_5m"
    ]
    geojson = json.loads(
        districts[geojson_cols].to_json()
    )
    return geojson, bounds


def _load_ps_boundaries(settings: Settings, ps_suff_df: pd.DataFrame | None = None) -> tuple[dict, dict, dict, list[dict], list[dict]]:
    if not settings.police_station_geojson.exists():
        return {"type": "FeatureCollection", "features": []}, {}, {}, [], []

    police_stations = gpd.read_file(settings.police_station_geojson).to_crs(epsg=4326)
    police_stations["map_district"] = police_stations["dst_nme"].astype(str).str.strip()
    police_stations["map_ps"]       = police_stations["ps"].astype(str).str.strip()
    police_stations["map_ps_key"]   = (
        police_stations["map_district"] + "||" + police_stations["map_ps"]
    )

    bounds:        dict[str, list]  = {}
    district_ps_map: dict[str, list] = {}
    ps_lookup:     list[dict]       = []
    ps_points:     list[dict]       = []

    # Map sufficiency statistics by PS Key (District||PS)
    suff_lookup = {}
    if ps_suff_df is not None:
        for row in ps_suff_df.itertuples():
            key = f"{row.district}||{row.police_station}"
            suff_lookup[key] = {
                "incidents": getattr(row, "incidents", 0),
                "current_frvs": getattr(row, "current_frvs", 0),
                "current_rt_min": getattr(row, "current_rt_min", 0.0),
                "req_frvs_10m": getattr(row, "req_frvs_10m", 0),
                "req_rt_10m": getattr(row, "req_rt_10m", 0.0),
                "add_frvs_10m": getattr(row, "add_frvs_10m", 0),
                "req_frvs_5m": getattr(row, "req_frvs_5m", 0),
                "req_rt_5m": getattr(row, "req_rt_5m", 0.0),
                "add_frvs_5m": getattr(row, "add_frvs_5m", 0),
            }

    for row in police_stations.itertuples():
        if not row.map_district or not row.map_ps:
            continue
        bounds[row.map_ps_key] = _geometry_bounds(row.geometry)
        district_ps_map.setdefault(row.map_district, []).append(
            {"label": row.map_ps, "value": row.map_ps_key}
        )
        point = row.geometry.representative_point()
        ps_lookup.append({
            "district": row.map_district,
            "ps":       row.map_ps,
            "psKey":    row.map_ps_key,
            "geometry": row.geometry,
        })
        
        # Pull stats from sufficiency lookup
        stats = suff_lookup.get(row.map_ps_key, {})
        
        ps_points.append({
            "lat":             float(point.y),
            "lon":             float(point.x),
            "district":        row.map_district,
            "ps":              row.map_ps,
            "psKey":           row.map_ps_key,
            "frvCount":        0,
            "nearestDistance": 0.0,
            "avgDistance":     0.0,
            
            # Capacity planning stats
            "incidents":       stats.get("incidents", 0),
            "current_frvs":    stats.get("current_frvs", 0),
            "current_rt_min":  stats.get("current_rt_min", None),
            "req_frvs_10m":    stats.get("req_frvs_10m", 0),
            "req_rt_10m":      stats.get("req_rt_10m", 0.0),
            "add_frvs_10m":    stats.get("add_frvs_10m", 0),
            "req_frvs_5m":     stats.get("req_frvs_5m", 0),
            "req_rt_5m":       stats.get("req_rt_5m", 0.0),
            "add_frvs_5m":     stats.get("add_frvs_5m", 0),
        })

    for items in district_ps_map.values():
        items.sort(key=lambda item: item["label"])

    # Attach stats to boundaries as well
    for col in ["current_frvs", "current_rt_min", "req_frvs_10m", "req_rt_10m", "req_frvs_5m", "req_rt_5m"]:
        police_stations[col] = police_stations["map_ps_key"].apply(
            lambda k: suff_lookup.get(k, {}).get(col, 0.0) if k in suff_lookup else 0.0
        )

    geojson_cols = ["map_district", "map_ps", "map_ps_key", "geometry",
                    "current_frvs", "current_rt_min", "req_frvs_10m", "req_rt_10m",
                    "req_frvs_5m", "req_rt_5m"]
    geojson = json.loads(
        police_stations[geojson_cols].to_json()
    )
    return geojson, bounds, district_ps_map, ps_lookup, ps_points


# ---------------------------------------------------------------------------
# Deployment / FRV helpers
# ---------------------------------------------------------------------------

def _build_deployment_points(medoids: dict, ps_lookup: list[dict]) -> list[dict]:
    frv_points: list[dict] = []

    for info in medoids.values():
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            continue

        point      = Point(float(lon), float(lat))
        matched_ps = _match_ps_for_point(point, str(info.get("district") or "").strip(), ps_lookup)
        district   = matched_ps["district"] if matched_ps else str(info.get("district") or "").strip()
        ps_name    = matched_ps["ps"]       if matched_ps else _deployment_ps_name(info)
        ps_key     = matched_ps["psKey"]    if matched_ps else (
            f"{district}||{ps_name}" if district and ps_name else ""
        )
        avg_response = float(info.get("avg_response_time_min") or 0)

        nearest_distance = info.get("road_factor_distance_to_nearest_ps_km")
        if nearest_distance is None:
            nearest_distance = info.get("distance_to_nearest_ps_km")

        frv_points.append({
            "lat":            float(lat),
            "lon":            float(lon),
            "district":       district,
            "ps":             ps_name,
            "psKey":          ps_key,
            "frvId":          str(info.get("frv_id") or "N/A"),
            "avgResponse":    avg_response,
            "maxResponse":    float(info.get("max_response_time_min") or 0),
            "incidents":      int(info.get("size") or 0),
            "nearestPs":      str(info.get("nearest_police_station") or "N/A"),
            "nearestDistance": float(nearest_distance) if nearest_distance is not None else None,
        })

    return frv_points


def _match_ps_for_point(point: Point, district: str, ps_lookup: list[dict]) -> dict | None:
    same_district  = [item for item in ps_lookup if item["district"] == district]
    other_district = [item for item in ps_lookup if item["district"] != district]
    for item in same_district + other_district:
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


def _deployment_ps_name(info: dict) -> str:
    for key in ("nearest_police_station", "police_station"):
        value = str(info.get(key) or "").strip()
        if value and value.upper() != "N/A":
            return value
    return ""


# ---------------------------------------------------------------------------
# Popup / formatting helpers  (kept here so map_data.json can carry pre-built
# popup HTML — avoids duplicating logic in JS)
# ---------------------------------------------------------------------------

def _build_frv_popup(info: dict) -> str:
    avg_resp    = float(info.get("avgResponse") or 0)
    max_resp    = float(info.get("maxResponse") or 0)
    resp_color  = "#159947" if avg_resp <= 10 else "#dc2626"
    nearest_ps  = escape(str(info.get("nearestPs") or "N/A"))
    nd          = info.get("nearestDistance")
    nearest_lbl = nearest_ps if nd is None else f"{nearest_ps} ({float(nd):.2f} km)"
    return (
        f'<div style="font-family:Segoe UI,Arial,sans-serif;min-width:285px;color:#17212b">'
        f'<h3 style="margin:0 0 8px 0;font-size:17px;color:{resp_color}">'
        f'FRV {escape(str(info.get("frvId", "N/A")))}</h3>'
        f'<table style="width:100%;font-size:13px;border-collapse:collapse">'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("district", "N/A")))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Police Station</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("ps") or "N/A"))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Nearest PS</b></td>'
        f'<td style="text-align:right">{nearest_lbl}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Incidents</b></td>'
        f'<td style="text-align:right">{int(info.get("incidents") or 0):,}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Max response</b></td>'
        f'<td style="text-align:right">{_format_minutes(max_resp)}</td></tr>'
        f'</table>'
        f'<div style="margin-top:10px;padding:8px;border-radius:6px;background:{resp_color};'
        f'color:#fff;text-align:center;font-weight:700">Avg response: {_format_minutes(avg_resp)}</div>'
        f'</div>'
    )


def _build_ps_popup(info: dict) -> str:
    return (
        f'<div style="font-family:Segoe UI,Arial,sans-serif;min-width:260px;color:#17212b">'
        f'<h3 style="margin:0 0 8px 0;font-size:17px;color:#1f6feb">Police Station</h3>'
        f'<table style="width:100%;font-size:13px;border-collapse:collapse">'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Name</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("ps") or "N/A"))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>District</b></td>'
        f'<td style="text-align:right">{escape(str(info.get("district") or "N/A"))}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>FRVs in PS</b></td>'
        f'<td style="text-align:right">{int(info.get("frvCount") or 0)}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Nearest FRV distance</b></td>'
        f'<td style="text-align:right">{float(info.get("nearestDistance") or 0):.2f} km</td></tr>'
        f'<tr><td style="padding:4px 0;color:#66717d"><b>Avg FRV distance</b></td>'
        f'<td style="text-align:right">{float(info.get("avgDistance") or 0):.2f} km</td></tr>'
        f'</table></div>'
    )


def _format_minutes(value: object) -> str:
    try:
        total_seconds = int(round(float(value) * 60))
    except Exception:
        total_seconds = 0
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# Geometry utilities
# ---------------------------------------------------------------------------

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


def _map_center(frv_points: list[dict], settings: Settings) -> tuple[float, float]:
    if not frv_points:
        return settings.map_center_lat, settings.map_center_lon
    return (
        sum(p["lat"] for p in frv_points) / len(frv_points),
        sum(p["lon"] for p in frv_points) / len(frv_points),
    )


def _data_output_path(map_html_path: Path) -> Path:
    """Return the path where map_data.json is written next to the HTML."""
    return map_html_path.parent / "map_data.json"