"""
Helpers for converting future FES capacity into extendable candidates.

The first-pass workflow keeps live existing assets as fixed baseline assets and
reinterprets future FES capacity as an upper-bound build envelope.

For renewables, the spatial FES split is reused directly as a local cap prior.
For future nuclear, workbook-derived large/small nuclear totals are combined
with site- and anchor-level placement logic in the thermal integration stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Mapping

import numpy as np
import pandas as pd


DEFAULT_FUTURE_CANDIDATE_CARRIERS = ("wind_onshore", "wind_offshore", "solar_pv", "nuclear")
DEFAULT_PLACEHOLDER_CAPITAL_COSTS = {
    "wind_onshore": 1200.0,
    "wind_offshore": 2500.0,
    "solar_pv": 700.0,
    "nuclear": 8000.0,
    "smr": 9500.0,
}
DEFAULT_FUTURE_NUCLEAR_CONFIG = {
    "enabled": True,
    "workbook_paths": {
        "2024": "data/FES/Future Energy Scenarios 2024 Data Workbook_V006_0.xlsx",
        "2025": "data/FES/Future Energy Scenarios 2025 Data Workbook V005_0.xlsx",
    },
    "en6_sites_path": "data/generators/nuclear_en6_sites.csv",
    "smr_anchors_path": "data/generators/smr_demand_anchors.csv",
    "smr_demand_weights": {
        "electricity": 1.0,
        "gas": 1.0,
    },
}

LAND_CARRIER_MAP = {
    "wind_onshore": ("onwind",),
    "wind_offshore": (
        "offwind-fixed-ac",
        "offwind-fixed-dc",
        "offwind-float-ac",
        "offwind-float-dc",
    ),
    "solar_pv": ("solar",),
    "smr": ("smr",),
    "nuclear": ("nuclear-large", "nuclear"),
}


def normalize_future_capacity_candidates_config(
    config: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return a normalized config dict with defaults filled in."""
    raw = dict(config or {})
    carriers = raw.get("carriers", DEFAULT_FUTURE_CANDIDATE_CARRIERS)
    if isinstance(carriers, str):
        carriers = [carriers]

    capital_costs = dict(DEFAULT_PLACEHOLDER_CAPITAL_COSTS)
    capital_costs.update(dict(raw.get("capital_costs", {}) or {}))
    nuclear_cfg = dict(DEFAULT_FUTURE_NUCLEAR_CONFIG)
    raw_nuclear = dict(raw.get("nuclear", {}) or {})
    nuclear_cfg["workbook_paths"] = {
        **DEFAULT_FUTURE_NUCLEAR_CONFIG["workbook_paths"],
        **dict(raw_nuclear.get("workbook_paths", {}) or {}),
    }
    nuclear_cfg["smr_demand_weights"] = {
        **DEFAULT_FUTURE_NUCLEAR_CONFIG["smr_demand_weights"],
        **dict(raw_nuclear.get("smr_demand_weights", {}) or {}),
    }
    for key in ("enabled", "en6_sites_path", "smr_anchors_path"):
        if key in raw_nuclear:
            nuclear_cfg[key] = raw_nuclear[key]

    return {
        "enabled": bool(raw.get("enabled", False)),
        "min_modelled_year": int(raw.get("min_modelled_year", 2025)),
        "supported_network_models": list(raw.get("supported_network_models", ["zonal"])),
        "carriers": [str(carrier) for carrier in carriers],
        "capital_costs": capital_costs,
        "nuclear": {
            "enabled": bool(nuclear_cfg.get("enabled", True)),
            "workbook_paths": {
                str(year): str(path)
                for year, path in dict(nuclear_cfg.get("workbook_paths", {}) or {}).items()
            },
            "en6_sites_path": str(nuclear_cfg.get("en6_sites_path", "")),
            "smr_anchors_path": str(nuclear_cfg.get("smr_anchors_path", "")),
            "smr_demand_weights": {
                "electricity": float(
                    dict(nuclear_cfg.get("smr_demand_weights", {}) or {}).get("electricity", 1.0)
                ),
                "gas": float(
                    dict(nuclear_cfg.get("smr_demand_weights", {}) or {}).get("gas", 1.0)
                ),
            },
        },
    }


def resolve_nuclear_workbook_path(
    fes_year: int | str,
    workbook_paths: Mapping[str, object],
) -> Path:
    """Resolve the configured workbook path for a selected FES year."""
    year_key = str(fes_year)
    if year_key not in workbook_paths:
        raise FileNotFoundError(
            f"No future_capacity_candidates.nuclear workbook path configured for FES year {fes_year}"
        )

    workbook_path = Path(str(workbook_paths[year_key]))
    if not workbook_path.exists():
        raise FileNotFoundError(
            f"Configured FES nuclear workbook does not exist for year {fes_year}: {workbook_path}"
        )
    return workbook_path


def _load_es1_table(workbook_path: str | Path) -> pd.DataFrame:
    """Load the ES1 table from a FES workbook with dynamic header detection."""
    raw = pd.read_excel(workbook_path, sheet_name="ES1", header=None)
    header_idx = None
    for idx, row in raw.iterrows():
        values = [str(value).strip() for value in row.tolist() if pd.notna(value)]
        if {"Connection", "Pathway", "Variable", "Category", "Type", "SubType"}.issubset(values):
            header_idx = idx
            break

    if header_idx is None:
        raise ValueError(f"Could not locate ES1 header row in workbook: {workbook_path}")

    header = raw.iloc[header_idx].tolist()
    table = raw.iloc[header_idx + 1 :].copy()
    table.columns = header
    table = table.dropna(how="all")
    table = table.rename(
        columns={
            "Connection": "connection",
            "Pathway": "pathway",
            "Variable": "variable",
            "Category": "category",
            "Type": "type",
            "SubType": "subtype",
        }
    )
    return table


def load_fes_nuclear_capacity_split(
    workbook_path: str | Path,
    modelled_year: int,
    fes_scenario: str,
) -> dict[str, float]:
    """
    Extract workbook-derived large/small nuclear capacities from ES1.

    Returns a dictionary with ``large_nuclear_mw`` and ``smr_mw`` totals.
    """
    table = _load_es1_table(workbook_path)
    year_col = None
    for candidate in (modelled_year, float(modelled_year), str(modelled_year)):
        if candidate in table.columns:
            year_col = candidate
            break
    if year_col is None:
        raise ValueError(
            f"Modelled year {modelled_year} is not present in ES1 workbook columns for {workbook_path}"
        )

    filtered = table[
        table["connection"].astype(str).str.casefold().eq("transmission")
        & table["pathway"].astype(str).str.casefold().eq(str(fes_scenario).casefold())
        & table["variable"].astype(str).str.casefold().eq("capacity (mw)")
        & table["type"].astype(str).str.casefold().eq("nuclear")
        & table["subtype"].astype(str).isin(["Nuclear - Large", "Nuclear - Small"])
    ].copy()
    if filtered.empty:
        raise ValueError(
            f"Workbook {workbook_path} does not contain ES1 nuclear large/small rows for scenario '{fes_scenario}'"
        )

    raw_capacity = filtered[year_col]
    numeric_capacity = pd.to_numeric(raw_capacity, errors="coerce")
    invalid_mask = numeric_capacity.isna()
    if invalid_mask.any():
        invalid_rows = filtered.loc[invalid_mask, ["pathway", "type", "subtype"]].copy()
        invalid_rows["raw_value"] = raw_capacity.loc[invalid_mask].astype(str).tolist()
        raise ValueError(
            "FES ES1 nuclear capacity contains non-numeric or missing values "
            f"for modelled year {modelled_year}, scenario '{fes_scenario}', "
            f"workbook {workbook_path}: {invalid_rows.to_dict('records')}"
        )

    negative_mask = numeric_capacity < 0
    if negative_mask.any():
        negative_rows = filtered.loc[negative_mask, ["pathway", "type", "subtype"]].copy()
        negative_rows["capacity_mw"] = numeric_capacity.loc[negative_mask].tolist()
        raise ValueError(
            "FES ES1 nuclear capacity contains negative values "
            f"for modelled year {modelled_year}, scenario '{fes_scenario}', "
            f"workbook {workbook_path}: {negative_rows.to_dict('records')}"
        )

    filtered["capacity_mw"] = numeric_capacity.astype(float)
    totals = filtered.groupby("subtype")["capacity_mw"].sum()

    return {
        "large_nuclear_mw": float(totals.get("Nuclear - Large", 0.0)),
        "smr_mw": float(totals.get("Nuclear - Small", 0.0)),
    }


def build_land_cap_table(technical_potential_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate technical-potential rows onto the network carrier names used in PyPSA.
    """
    if technical_potential_df.empty:
        return pd.DataFrame(columns=["carrier", "zone_name", "land_cap_mw"])

    rows: list[dict[str, object]] = []
    for network_carrier, csv_carriers in LAND_CARRIER_MAP.items():
        matched = technical_potential_df[technical_potential_df["carrier"].isin(csv_carriers)].copy()
        if matched.empty:
            continue
        grouped = (
            matched.groupby("zone_name", dropna=False)["p_nom_max_mw"]
            .sum()
            .reset_index(name="land_cap_mw")
        )
        grouped["carrier"] = network_carrier
        rows.extend(grouped.to_dict("records"))

    if not rows:
        return pd.DataFrame(columns=["carrier", "zone_name", "land_cap_mw"])

    land_caps = pd.DataFrame(rows)
    return land_caps[["carrier", "zone_name", "land_cap_mw"]]


def aggregate_capacity_by_carrier_bus(
    df: pd.DataFrame,
    carrier_col: str,
    capacity_col: str = "capacity_mw",
) -> pd.DataFrame:
    """Aggregate capacity data to one row per (carrier, bus)."""
    if df.empty:
        return pd.DataFrame(
            columns=["carrier", "bus", "lat", "lon", "capacity_mw"]
        )

    working = df.copy()
    working = working[working["bus"].notna()].copy()
    if working.empty:
        return pd.DataFrame(
            columns=["carrier", "bus", "lat", "lon", "capacity_mw"]
        )

    working["carrier"] = working[carrier_col].astype(str)

    def _weighted_mean(series: pd.Series, weights: pd.Series) -> float:
        valid = series.notna() & weights.notna()
        if not valid.any():
            return np.nan
        valid_weights = weights[valid].astype(float)
        if valid_weights.sum() <= 0:
            return float(series[valid].astype(float).mean())
        return float(np.average(series[valid].astype(float), weights=valid_weights))

    records = []
    for (carrier, bus), group in working.groupby(["carrier", "bus"], dropna=False):
        weights = group[capacity_col].astype(float)
        records.append(
            {
                "carrier": carrier,
                "bus": bus,
                "lat": _weighted_mean(group["lat"], weights) if "lat" in group.columns else np.nan,
                "lon": _weighted_mean(group["lon"], weights) if "lon" in group.columns else np.nan,
                "capacity_mw": float(weights.sum()),
            }
        )

    return pd.DataFrame.from_records(records)


def build_candidate_headroom_table(
    fes_capacity_df: pd.DataFrame,
    baseline_capacity_df: pd.DataFrame,
    candidate_carriers: Iterable[str],
    land_caps_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Combine FES caps, live baseline capacity, and optional land-cap metadata.

    The returned table is a pre-policy audit surface. It may calculate land-aware
    diagnostic fields, but the assembled network's final candidate ``p_nom_max``
    is tightened downstream by ``apply_technical_potential_constraints.py``.
    """
    candidate_carriers = list(candidate_carriers)
    fes_filtered = fes_capacity_df[fes_capacity_df["carrier"].isin(candidate_carriers)].copy()
    base_filtered = baseline_capacity_df[
        baseline_capacity_df["carrier"].isin(candidate_carriers)
    ].copy()

    headroom = fes_filtered.rename(
        columns={"capacity_mw": "fes_spatial_cap_mw"}
    )[["carrier", "bus", "lat", "lon", "fes_spatial_cap_mw"]]

    live_existing = base_filtered.rename(
        columns={"capacity_mw": "live_existing_capacity_mw"}
    )[["carrier", "bus", "live_existing_capacity_mw"]]

    headroom = headroom.merge(live_existing, on=["carrier", "bus"], how="left")
    headroom["live_existing_capacity_mw"] = (
        headroom["live_existing_capacity_mw"].fillna(0.0).astype(float)
    )
    headroom["zone_name"] = headroom["bus"].astype(str)

    if land_caps_df is not None and not land_caps_df.empty:
        headroom = headroom.merge(
            land_caps_df[["carrier", "zone_name", "land_cap_mw"]],
            on=["carrier", "zone_name"],
            how="left",
        )
    else:
        headroom["land_cap_mw"] = np.nan

    headroom["effective_total_cap_mw"] = np.where(
        headroom["land_cap_mw"].notna(),
        np.minimum(headroom["fes_spatial_cap_mw"], headroom["land_cap_mw"]),
        headroom["fes_spatial_cap_mw"],
    )
    headroom["extendable_headroom_pre_land_mw"] = np.maximum(
        headroom["fes_spatial_cap_mw"] - headroom["live_existing_capacity_mw"],
        0.0,
    )
    headroom["extendable_headroom_mw"] = np.maximum(
        headroom["effective_total_cap_mw"] - headroom["live_existing_capacity_mw"],
        0.0,
    )
    headroom["land_binding"] = (
        headroom["land_cap_mw"].notna()
        & (headroom["land_cap_mw"] < (headroom["fes_spatial_cap_mw"] - 1e-9))
    )
    headroom["oversubscribed"] = (
        headroom["live_existing_capacity_mw"] > headroom["effective_total_cap_mw"]
    )
    headroom["existing_oversubscribed"] = headroom["oversubscribed"]
    headroom["oversubscription_amount_mw"] = np.where(
        headroom["oversubscribed"],
        headroom["live_existing_capacity_mw"] - headroom["effective_total_cap_mw"],
        0.0,
    )
    headroom["possible_preallocation_artifact"] = (
        headroom["land_binding"]
        & (headroom["extendable_headroom_pre_land_mw"] > 1e-9)
        & (headroom["extendable_headroom_mw"] <= 1e-9)
    )
    return headroom.sort_values(["carrier", "zone_name", "bus"]).reset_index(drop=True)


def build_candidate_rows(
    headroom_df: pd.DataFrame,
    modelled_year: int,
    capital_costs: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """
    Create one extendable candidate row per (carrier, bus) with positive headroom.
    """
    merged_capital_costs = dict(DEFAULT_PLACEHOLDER_CAPITAL_COSTS)
    merged_capital_costs.update(dict(capital_costs or {}))
    candidates = headroom_df[headroom_df["extendable_headroom_pre_land_mw"] > 0].copy()
    if candidates.empty:
        return pd.DataFrame(
            columns=[
                "site_name",
                "technology",
                "capacity_mw",
                "bus",
                "lat",
                "lon",
                "data_source",
                "p_nom_extendable",
                "p_nom_min",
                "p_nom_max",
                "capital_cost",
                "future_candidate_fes_spatial_cap_mw",
                "future_candidate_live_existing_capacity_mw",
                "future_candidate_zonal_land_cap_mw",
            ]
        )

    rows = []
    for _, row in candidates.iterrows():
        carrier = str(row["carrier"])
        bus = str(row["bus"])

        def _optional_float(column: str) -> float:
            value = row.get(column, np.nan)
            return float(value) if pd.notna(value) else np.nan

        rows.append(
            {
                "site_name": f"FESCandidate_{carrier}_{bus}_{modelled_year}",
                "technology": carrier,
                "capacity_mw": 0.0,
                "bus": bus,
                "lat": row.get("lat", np.nan),
                "lon": row.get("lon", np.nan),
                "data_source": "FES_candidate",
                "p_nom_extendable": True,
                "p_nom_min": 0.0,
                "p_nom_max": float(row["extendable_headroom_pre_land_mw"]),
                "capital_cost": float(merged_capital_costs.get(carrier, 0.0)),
                "future_candidate_fes_spatial_cap_mw": _optional_float("fes_spatial_cap_mw"),
                "future_candidate_live_existing_capacity_mw": _optional_float(
                    "live_existing_capacity_mw"
                ),
                "future_candidate_zonal_land_cap_mw": _optional_float("land_cap_mw"),
            }
        )

    return pd.DataFrame.from_records(rows)
