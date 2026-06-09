from __future__ import annotations

import logging

from .cleaning import load_and_clean_incidents, write_cleaning_audit
from .clustering import run_district_level_clustering
from .config import Settings
from .geo import assign_boundaries
from .ingestion import combine_raw_lbs_files, resolve_combined_csv
from .clean_mapping import generate_map

logger = logging.getLogger(__name__)


def run_pipeline(settings: Settings, skip_ingest: bool = False, incremental: bool = False, skip_map: bool = False) -> dict:
    settings.ensure_dirs()

    if skip_ingest:
        combined_csv = resolve_combined_csv(settings)
        logger.info("Skipping ingest; using %s", combined_csv)
    else:
        combined_csv = combine_raw_lbs_files(settings, incremental=incremental)

    incidents, audit = load_and_clean_incidents(combined_csv, settings.analysis_window_days)
    if len(incidents) < settings.min_cluster_size:
        raise RuntimeError(f"Only {len(incidents)} valid incidents available after cleaning")

    incidents = assign_boundaries(incidents, settings)
    clustered, medoids, district_summaries = run_district_level_clustering(incidents, settings)
    write_cleaning_audit(audit, settings.cleaning_audit_csv)

    map_path = None
    if not skip_map:
        map_path = generate_map(settings)

    return {
        "n_total": len(clustered),
        "n_medoids": len(medoids),
        "n_noise": int((clustered["cluster_label"] == -1).sum()),
        "districts": len(district_summaries),
        "clustered_csv": str(settings.clustered_csv),
        "medoids_json": str(settings.medoids_json),
        "map_html": map_path,
    }
