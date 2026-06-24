from __future__ import annotations

import argparse
import logging

from .config import get_settings
from .ingestion import combine_raw_lbs_files
from .clean_mapping import generate_map
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Production LBS Predictor")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run full district-wise FRV optimization pipeline")
    run.add_argument("--skip-ingest", action="store_true", help="Use existing combined CSV")
    run.add_argument("--incremental", action="store_true", help="Only ingest raw files not seen before")
    run.add_argument("--skip-map", action="store_true", help="Do not generate Folium map")
    run.add_argument("--days", type=int, default=None, help="Only analyze incidents from last N days")
    run.add_argument("--min-cluster", type=int, default=None, help="Override HDBSCAN min_cluster_size")
    run.add_argument("--min-samples", type=int, default=None, help="Override HDBSCAN min_samples")

    ingest = subparsers.add_parser("ingest", help="Combine raw LBS CSVs into one processed CSV")
    ingest.add_argument("--incremental", action="store_true", help="Only ingest raw files not seen before")

    subparsers.add_parser("map", help="Regenerate map from existing output CSV/JSON")
    
    sufficiency = subparsers.add_parser("sufficiency", help="Run bottom-up FRV sufficiency and capacity planning")
    sufficiency.add_argument("--min-utility", type=float, default=2000.0, help="Minimum total response time minutes saved to justify adding an FRV (default: 2000)")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    if getattr(args, "days", None) is not None:
        settings.analysis_window_days = args.days
    if getattr(args, "min_cluster", None) is not None:
        settings.min_cluster_size = args.min_cluster
    if getattr(args, "min_samples", None) is not None:
        settings.min_samples = args.min_samples

    if args.command == "run":
        result = run_pipeline(settings, skip_ingest=args.skip_ingest, incremental=args.incremental, skip_map=args.skip_map)
        print("Pipeline complete")
        for key, value in result.items():
            print(f"{key}: {value}")
    elif args.command == "ingest":
        path = combine_raw_lbs_files(settings, incremental=args.incremental)
        print(f"Combined CSV: {path}")
    elif args.command == "map":
        path = generate_map(settings)
        print(f"Map: {path}")
    elif args.command == "sufficiency":
        from .sufficiency import run_sufficiency_analysis
        result = run_sufficiency_analysis(settings, min_utility_mins=args.min_utility)
        print("Sufficiency planning complete")
        print("State Summary:")
        for key, value in result["state_results"].items():
            print(f"  {key}: {value}")
        print(f"PS Summary: {result['ps_summary']}")
        print(f"District Summary: {result['district_summary']}")
        print(f"State Summary: {result['state_summary']}")
        print(f"Placements: {result['placements']}")


if __name__ == "__main__":
    main()
