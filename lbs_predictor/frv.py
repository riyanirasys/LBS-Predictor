from __future__ import annotations

import pandas as pd

from .config import Settings


def normalize_name(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).lower().strip()


def load_frv_allocations(settings: Settings) -> tuple[pd.DataFrame, dict[str, int], dict[str, list[str]]]:
    frv_df = pd.read_csv(settings.frv_master_csv)
    frv_df = frv_df[~frv_df["UnitID"].astype(str).str.contains("TEST", case=False, na=False)].copy()

    mapping = pd.read_csv(settings.district_mapping_csv)
    code_to_name = dict(zip(mapping["UnitID"], mapping["UnitCallSign"]))

    frv_df["district"] = frv_df["UnitDistrict"].map(code_to_name)
    frv_df = frv_df.rename(columns={"UnitBaseLocation_X": "base_lat", "UnitBaseLocation_Y": "base_lon"})

    district_counts: dict[str, int] = {}
    frv_by_district: dict[str, list[str]] = {}
    for district, group in frv_df.dropna(subset=["district"]).groupby("district"):
        district_counts[district] = len(group)
        frv_by_district[district] = group["UnitID"].astype(str).tolist()

    return frv_df, district_counts, frv_by_district
