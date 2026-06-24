from __future__ import annotations

import logging
from pathlib import Path
import json

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from sklearn.metrics.pairwise import haversine_distances

from .config import Settings
from .frv import normalize_name, load_frv_allocations
from .clustering import compute_medoid

logger = logging.getLogger(__name__)


def map_frvs_to_police_stations(frv_df: pd.DataFrame, ps_gdf: gpd.GeoDataFrame) -> dict[str, str]:
    """
    Maps each UnitID to a matched PS name from the GeoJSON.
    Uses spatial join first, then falls back to name matching.
    """
    mapping: dict[str, str] = {}
    
    frv_co = frv_df.copy()
    frv_co["base_lat"] = pd.to_numeric(frv_co["base_lat"], errors="coerce")
    frv_co["base_lon"] = pd.to_numeric(frv_co["base_lon"], errors="coerce")
    
    valid_coords = frv_co.dropna(subset=["base_lat", "base_lon"])
    valid_coords = valid_coords[(valid_coords["base_lat"] != 0) & (valid_coords["base_lon"] != 0)]
    
    if not valid_coords.empty and not ps_gdf.empty:
        # Point geometry in EPSG:4326 format: (longitude, latitude)
        geometry = [Point(lon, lat) for lon, lat in zip(valid_coords["base_lon"], valid_coords["base_lat"])]
        frv_gdf = gpd.GeoDataFrame(valid_coords.copy(), geometry=geometry, crs="EPSG:4326")
        
        if ps_gdf.crs is None:
            ps_gdf = ps_gdf.set_crs("EPSG:4326")
        elif ps_gdf.crs != frv_gdf.crs:
            ps_gdf = ps_gdf.to_crs("EPSG:4326")
            
        joined = gpd.sjoin(frv_gdf, ps_gdf[["ps", "geometry"]], how="left", predicate="within")
        for row in joined.itertuples():
            if pd.notna(row.ps):
                mapping[str(row.UnitID)] = str(row.ps)
                
    # Fallback to name matching
    ps_names = ps_gdf["ps"].dropna().unique() if not ps_gdf.empty else []
    lookup = {normalize_name(name): name for name in ps_names}
    
    for row in frv_co.itertuples():
        unit_id = str(row.UnitID)
        if unit_id in mapping:
            continue
        
        raw_ps = row.UnitPoliceStation
        norm_ps = normalize_name(raw_ps)
        if norm_ps in lookup:
            mapping[unit_id] = lookup[norm_ps]
            continue
            
        # Substring matching fallback
        matched = False
        for k, v in lookup.items():
            if k in norm_ps or norm_ps in k:
                mapping[unit_id] = v
                matched = True
                break
        if matched:
            continue
            
        mapping[unit_id] = "Outside PS"
        
    return mapping


def get_current_frv_counts(settings: Settings) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """
    Loads baseline FRV counts per Police Station and District.
    """
    frv_df, district_counts_base, _ = load_frv_allocations(settings)
    
    if settings.police_station_geojson.exists():
        ps_gdf = gpd.read_file(settings.police_station_geojson)
    else:
        ps_gdf = gpd.GeoDataFrame()
        
    frv_to_ps = map_frvs_to_police_stations(frv_df, ps_gdf)
    frv_df["ps"] = frv_df["UnitID"].astype(str).map(frv_to_ps)
    
    frv_df["ps_key"] = frv_df["district"] + "||" + frv_df["ps"]
    ps_counts = frv_df.groupby("ps_key").size().to_dict()
    district_counts = frv_df.groupby("district").size().to_dict()
    
    return ps_counts, district_counts, frv_to_ps


def calculate_average_response_time(
    coords: np.ndarray,
    weights: np.ndarray,
    medoids: np.ndarray,
    settings: Settings,
) -> float:
    """
    Computes weighted average travel time from medoids to coords using road factor.
    """
    if len(coords) == 0 or len(medoids) == 0:
        return float("inf")
        
    coords_rad = np.radians(coords)
    medoids_rad = np.radians(medoids)
    
    dist_matrix = haversine_distances(coords_rad, medoids_rad) * settings.earth_radius_km
    min_dists = dist_matrix.min(axis=1)
    
    road_dists = min_dists * settings.road_factor
    travel_times = (road_dists / settings.frv_avg_speed_kph) * 60.0
    
    total_weight = weights.sum()
    if total_weight == 0:
        return 0.0
    return float(np.sum(travel_times * weights) / total_weight)


def optimize_placements(
    coords: np.ndarray,
    weights: np.ndarray,
    v: int,
    settings: Settings,
) -> tuple[np.ndarray, float]:
    """
    Partitions demand coordinates into v clusters and returns their medoids and average RT.
    """
    if v <= 0:
        return np.empty((0, 2)), float("inf")
    if len(coords) <= v:
        return coords, 0.0
        
    from sklearn.cluster import KMeans
    kmeans = KMeans(n_clusters=v, random_state=42, n_init=3)
    labels = kmeans.fit_predict(coords, sample_weight=weights)
    
    medoids_list = []
    for label in range(v):
        mask = (labels == label)
        if not mask.any():
            continue
        sub_coords = coords[mask]
        sub_weights = weights[mask]
        med = compute_medoid(sub_coords, sub_weights, settings)
        medoids_list.append(med)
        
    medoids = np.array(medoids_list)
    avg_rt = calculate_average_response_time(coords, weights, medoids, settings)
    return medoids, avg_rt


def solve_sufficiency(
    coords: np.ndarray,
    weights: np.ndarray,
    v_curr: int,
    target_rt: float,
    settings: Settings,
    max_frvs: int = 30,
    min_utility_mins: float = 2000.0,
) -> tuple[int, np.ndarray, float]:
    """
    Finds the minimum number of FRVs required to hit the target_rt.
    """
    start_v = max(1, v_curr)
    
    # 1. Evaluate baseline
    medoids, avg_rt = optimize_placements(coords, weights, start_v, settings)
    if avg_rt <= target_rt or start_v >= len(coords):
        return start_v, medoids, avg_rt
        
    n_incidents = weights.sum()
    prev_rt = avg_rt
    prev_medoids = medoids
    
    # 2. Search upwards
    for v in range(start_v + 1, max_frvs + 1):
        medoids, avg_rt = optimize_placements(coords, weights, v, settings)
        
        # Calculate total travel time saved in minutes across all incidents
        time_saved = n_incidents * (prev_rt - avg_rt)
        
        # Check if the utility is below the threshold
        if time_saved < min_utility_mins:
            return v - 1, prev_medoids, prev_rt
            
        if avg_rt <= target_rt or v >= len(coords):
            return v, medoids, avg_rt
            
        prev_rt = avg_rt
        prev_medoids = medoids
        
    return max_frvs, medoids, avg_rt


def get_medoid_details(
    coords: np.ndarray,
    weights: np.ndarray,
    medoids: np.ndarray,
    settings: Settings,
) -> list[dict]:
    """
    Computes the size (incidents), average travel time, and max travel time for each medoid.
    """
    if len(coords) == 0 or len(medoids) == 0:
        return []
        
    coords_rad = np.radians(coords)
    medoids_rad = np.radians(medoids)
    
    # Distance matrix between demand points and medoids (in km)
    dist_matrix = haversine_distances(coords_rad, medoids_rad) * settings.earth_radius_km
    
    # For each demand point, find the nearest medoid index
    nearest_idx = dist_matrix.argmin(axis=1)
    
    # Travel times for all points to all medoids (in minutes)
    road_dists = dist_matrix * settings.road_factor
    travel_times = (road_dists / settings.frv_avg_speed_kph) * 60.0
    
    details = []
    for j in range(len(medoids)):
        mask = (nearest_idx == j)
        if not mask.any():
            details.append({
                "incidents": 0,
                "avg_rt": 0.0,
                "max_rt": 0.0
            })
            continue
            
        sub_weights = weights[mask]
        sub_times = travel_times[mask, j]
        
        size = int(sub_weights.sum())
        avg_rt = float(np.sum(sub_times * sub_weights) / size) if size > 0 else 0.0
        max_rt = float(sub_times.max()) if size > 0 else 0.0
        
        details.append({
            "incidents": size,
            "avg_rt": round(avg_rt, 2),
            "max_rt": round(max_rt, 2)
        })
    return details


def run_sufficiency_analysis(settings: Settings, min_utility_mins: float = 2000.0) -> dict:
    """
    Main sufficiency pipeline.
    """
    settings.ensure_dirs()
    
    if not settings.clustered_csv.exists():
        raise FileNotFoundError(
            f"Clustered data not found at {settings.clustered_csv}. Please run pipeline first."
        )
        
    logger.info("Loading incident data from %s", settings.clustered_csv)
    df = pd.read_csv(settings.clustered_csv)
    
    # Drop rows outside boundaries
    df = df[(df["ps"].notna()) & (df["ps"] != "Outside PS")]
    df = df[(df["district"].notna()) & (df["district"] != "Outside MP")]
    
    logger.info("Loading current FRV allocations...")
    ps_frv_counts, _, _ = get_current_frv_counts(settings)
    
    ps_results = []
    placement_records = []
    
    grouped = list(df.groupby(["district", "ps"]))
    logger.info("Analyzing %s Police Stations...", len(grouped))
    
    for ps_idx, ((district, ps), group) in enumerate(sorted(grouped, key=lambda x: x[0]), 1):
        # 1. Aggregate incidents to grid cells
        group_co = group.copy()
        group_co["lat_grid"] = group_co["latitude"].round(3)
        group_co["lon_grid"] = group_co["longitude"].round(3)
        grid = group_co.groupby(["lat_grid", "lon_grid"]).size().reset_index(name="weight")
        
        coords = grid[["lat_grid", "lon_grid"]].to_numpy()
        weights = grid["weight"].to_numpy()
        n_incidents = len(group)
        
        ps_key = f"{district}||{ps}"
        v_curr = ps_frv_counts.get(ps_key, 0)
        
        # Safe checks for tiny data
        if n_incidents < 10 or len(coords) == 0:
            ps_results.append({
                "district": district,
                "police_station": ps,
                "incidents": n_incidents,
                "current_frvs": v_curr,
                "current_rt_min": 0.0,
                "req_frvs_10m": v_curr,
                "req_rt_10m": 0.0,
                "add_frvs_10m": 0,
                "req_frvs_5m": v_curr,
                "req_rt_5m": 0.0,
                "add_frvs_5m": 0,
            })
            continue
            
        # Solve current RT
        current_medoids, current_rt = optimize_placements(coords, weights, v_curr, settings) if v_curr > 0 else (np.empty((0, 2)), float("inf"))
        
        # Solve targets with optimized default thresholds (5000.0 for 10m, 1000.0 for 5m)
        u_10 = min_utility_mins if min_utility_mins != 2000.0 else 5000.0
        u_5 = min_utility_mins if min_utility_mins != 2000.0 else 1000.0

        # Solve 10 min target
        v_10, medoids_10, rt_10 = solve_sufficiency(coords, weights, v_curr, 10.0, settings, min_utility_mins=u_10)
        # Solve 5 min target
        v_5, medoids_5, rt_5 = solve_sufficiency(coords, weights, v_curr, 5.0, settings, min_utility_mins=u_5)
        
        ps_results.append({
            "district": district,
            "police_station": ps,
            "incidents": n_incidents,
            "current_frvs": v_curr,
            "current_rt_min": round(current_rt, 2) if current_rt != float("inf") else None,
            "req_frvs_10m": v_10,
            "req_rt_10m": round(rt_10, 2),
            "add_frvs_10m": max(0, v_10 - v_curr),
            "req_frvs_5m": v_5,
            "req_rt_5m": round(rt_5, 2),
            "add_frvs_5m": max(0, v_5 - v_curr),
        })
        
        # Get details for each medoid
        details_curr = get_medoid_details(coords, weights, current_medoids, settings)
        details_10 = get_medoid_details(coords, weights, medoids_10, settings)
        details_5 = get_medoid_details(coords, weights, medoids_5, settings)

        # Save placement coordinates
        for idx, ((lat, lon), det) in enumerate(zip(current_medoids, details_curr), 1):
            placement_records.append({
                "district": district,
                "police_station": ps,
                "target": "current",
                "placement_id": idx,
                "latitude": round(float(lat), 6),
                "longitude": round(float(lon), 6),
                "incidents": det["incidents"],
                "avg_response": det["avg_rt"],
                "max_response": det["max_rt"],
            })
            
        for idx, ((lat, lon), det) in enumerate(zip(medoids_10, details_10), 1):
            placement_records.append({
                "district": district,
                "police_station": ps,
                "target": "10m",
                "placement_id": idx,
                "latitude": round(float(lat), 6),
                "longitude": round(float(lon), 6),
                "incidents": det["incidents"],
                "avg_response": det["avg_rt"],
                "max_response": det["max_rt"],
            })
            
        for idx, ((lat, lon), det) in enumerate(zip(medoids_5, details_5), 1):
            placement_records.append({
                "district": district,
                "police_station": ps,
                "target": "5m",
                "placement_id": idx,
                "latitude": round(float(lat), 6),
                "longitude": round(float(lon), 6),
                "incidents": det["incidents"],
                "avg_response": det["avg_rt"],
                "max_response": det["max_rt"],
            })
            
        if ps_idx % 50 == 0 or ps_idx == len(grouped):
            logger.info("Processed %s/%s police stations...", ps_idx, len(grouped))
            
    ps_df = pd.DataFrame(ps_results)
    
    # 2. Aggregate to District Level (Bottom-Up)
    dist_results = []
    for district, group in ps_df.groupby("district"):
        total_inc = group["incidents"].sum()
        
        # Weighted RTs
        def weighted_rt(rt_col):
            sub = group.dropna(subset=[rt_col])
            if sub.empty or total_inc == 0:
                return 0.0
            return round((sub[rt_col] * sub["incidents"]).sum() / sub["incidents"].sum(), 2)
            
        dist_results.append({
            "district": district,
            "incidents": total_inc,
            "current_frvs": group["current_frvs"].sum(),
            "current_rt_min": weighted_rt("current_rt_min"),
            "req_frvs_10m": group["req_frvs_10m"].sum(),
            "req_rt_10m": weighted_rt("req_rt_10m"),
            "add_frvs_10m": group["add_frvs_10m"].sum(),
            "req_frvs_5m": group["req_frvs_5m"].sum(),
            "req_rt_5m": weighted_rt("req_rt_5m"),
            "add_frvs_5m": group["add_frvs_5m"].sum(),
        })
    dist_df = pd.DataFrame(dist_results)
    
    # 3. Aggregate to State Level (Bottom-Up)
    state_total_inc = dist_df["incidents"].sum()
    
    def state_weighted_rt(rt_col):
        sub = dist_df.dropna(subset=[rt_col])
        if sub.empty or state_total_inc == 0:
            return 0.0
        return round((sub[rt_col] * sub["incidents"]).sum() / state_total_inc, 2)
        
    state_results = [{
        "incidents": state_total_inc,
        "current_frvs": dist_df["current_frvs"].sum(),
        "current_rt_min": state_weighted_rt("current_rt_min"),
        "req_frvs_10m": dist_df["req_frvs_10m"].sum(),
        "req_rt_10m": state_weighted_rt("req_rt_10m"),
        "add_frvs_10m": dist_df["add_frvs_10m"].sum(),
        "req_frvs_5m": dist_df["req_frvs_5m"].sum(),
        "req_rt_5m": state_weighted_rt("req_rt_5m"),
        "add_frvs_5m": dist_df["add_frvs_5m"].sum(),
    }]
    state_df = pd.DataFrame(state_results)
    
    # Save outputs
    ps_summary_path = settings.output_dir / "ps_sufficiency_summary.csv"
    dist_summary_path = settings.output_dir / "district_sufficiency_summary.csv"
    state_summary_path = settings.output_dir / "state_sufficiency_summary.csv"
    placements_path = settings.output_dir / "sufficiency_placements.csv"
    
    ps_df.to_csv(ps_summary_path, index=False)
    dist_df.to_csv(dist_summary_path, index=False)
    state_df.to_csv(state_summary_path, index=False)
    pd.DataFrame(placement_records).to_csv(placements_path, index=False)
    
    logger.info("Saved PS summary to %s", ps_summary_path)
    logger.info("Saved district summary to %s", dist_summary_path)
    logger.info("Saved state summary to %s", state_summary_path)
    logger.info("Saved optimal placements to %s", placements_path)
    
    return {
        "ps_summary": str(ps_summary_path),
        "district_summary": str(dist_summary_path),
        "state_summary": str(state_summary_path),
        "placements": str(placements_path),
        "state_results": state_results[0],
    }
