"""
Helpers for future nuclear candidate construction.

These functions keep the thermal integration stage focused on workflow wiring
while the nuclear-specific logic stays testable and auditable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd


def normalize_site_token(value: object) -> str:
    """Normalize site or anchor names for stable matching."""
    text = "" if value is None else str(value)
    lowered = text.casefold()
    for char in (" ", "-", "_", ",", ".", "(", ")"):
        lowered = lowered.replace(char, "")
    return lowered


def load_en6_sites(csv_path: str | Path) -> pd.DataFrame:
    """Load repo-local EN-6 large-nuclear site metadata."""
    df = pd.read_csv(csv_path)
    required = {
        "site_id",
        "site_name",
        "site_status",
        "lat",
        "lon",
        "country",
        "existing_name_patterns",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"EN-6 site table missing required columns: {sorted(missing)}")
    return df.copy()


def load_smr_anchors(csv_path: str | Path) -> pd.DataFrame:
    """Load repo-local SMR anchor data."""
    df = pd.read_csv(csv_path)
    required = {
        "anchor_id",
        "anchor_name",
        "anchor_type",
        "lat",
        "lon",
        "electricity_demand_mw",
        "gas_demand_mw",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"SMR anchor table missing required columns: {sorted(missing)}")
    return df.copy()


def assign_existing_sites_to_en6(
    existing_sites_df: pd.DataFrame,
    en6_sites_df: pd.DataFrame,
) -> pd.DataFrame:
    """Assign live existing nuclear sites to EN-6 site ids using configured name patterns."""
    if existing_sites_df.empty:
        return pd.DataFrame(columns=["site_id", "live_existing_capacity_mw"])

    pattern_rows = []
    for _, row in en6_sites_df.iterrows():
        raw_patterns = str(row.get("existing_name_patterns", "") or "")
        patterns = [normalize_site_token(pattern) for pattern in raw_patterns.split("|") if pattern.strip()]
        if not patterns:
            patterns = [normalize_site_token(row["site_name"])]
        pattern_rows.append((str(row["site_id"]), patterns))

    assignments = []
    for _, row in existing_sites_df.iterrows():
        normalized_name = normalize_site_token(row.get("site_name"))
        matched_site_id = None
        for site_id, patterns in pattern_rows:
            if any(pattern and pattern in normalized_name for pattern in patterns):
                matched_site_id = site_id
                break
        if matched_site_id is None:
            continue
        assignments.append(
            {
                "site_id": matched_site_id,
                "capacity_mw": float(pd.to_numeric(row.get("capacity_mw"), errors="coerce") or 0.0),
            }
        )

    if not assignments:
        return pd.DataFrame(columns=["site_id", "live_existing_capacity_mw"])

    assigned = pd.DataFrame(assignments)
    return (
        assigned.groupby("site_id", dropna=False)["capacity_mw"]
        .sum()
        .reset_index(name="live_existing_capacity_mw")
    )


def build_large_nuclear_headroom_table(
    en6_sites_df: pd.DataFrame,
    existing_sites_df: pd.DataFrame,
    site_cap_mw: float,
    scotland_ban: bool = True,
) -> pd.DataFrame:
    """Build site-level large-nuclear candidate headroom."""
    sites = en6_sites_df.copy()
    if scotland_ban and "country" in sites.columns:
        sites = sites[sites["country"].astype(str).str.casefold() != "scotland"].copy()

    existing_by_site = assign_existing_sites_to_en6(existing_sites_df, sites)
    sites = sites.merge(existing_by_site, on="site_id", how="left")
    sites["live_existing_capacity_mw"] = (
        pd.to_numeric(sites["live_existing_capacity_mw"], errors="coerce").fillna(0.0)
    )
    sites["local_siting_cap_mw"] = float(site_cap_mw)
    sites["local_headroom_mw"] = np.maximum(
        sites["local_siting_cap_mw"] - sites["live_existing_capacity_mw"],
        0.0,
    )
    sites["oversubscribed"] = sites["live_existing_capacity_mw"] > sites["local_siting_cap_mw"]
    sites["oversubscription_amount_mw"] = np.where(
        sites["oversubscribed"],
        sites["live_existing_capacity_mw"] - sites["local_siting_cap_mw"],
        0.0,
    )
    return sites.reset_index(drop=True)


def _weighted_zone_shares(
    df: pd.DataFrame,
    value_col: str,
    zone_col: str = "zone_name",
    score_col: str = "demand_score",
) -> pd.Series:
    """Split a zone total across rows using score weights, falling back to equal shares."""
    shares = pd.Series(0.0, index=df.index, dtype=float)
    for _, group in df.groupby(zone_col, dropna=False):
        total_value = float(group[value_col].iloc[0])
        scores = pd.to_numeric(group[score_col], errors="coerce").fillna(0.0)
        if total_value <= 0 or len(group) == 0:
            shares.loc[group.index] = 0.0
            continue
        if float(scores.sum()) > 0:
            weights = scores / float(scores.sum())
        else:
            weights = pd.Series(1.0 / len(group), index=group.index)
        shares.loc[group.index] = weights * total_value
    return shares


def build_smr_candidate_headroom_table(
    anchors_df: pd.DataFrame,
    zone_land_caps_df: pd.DataFrame,
    national_fes_total_mw: float,
    demand_weights: Mapping[str, float],
    zone_col: str = "zone_name",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Allocate national FES SMR headroom to eligible anchors and zonal land caps."""
    anchors = anchors_df.copy()
    if anchors.empty:
        return anchors, pd.DataFrame()

    electricity_weight = float(demand_weights.get("electricity", 1.0))
    gas_weight = float(demand_weights.get("gas", 1.0))
    anchors["demand_score"] = (
        pd.to_numeric(anchors["electricity_demand_mw"], errors="coerce").fillna(0.0) * electricity_weight
        + pd.to_numeric(anchors["gas_demand_mw"], errors="coerce").fillna(0.0) * gas_weight
    )

    land_caps = zone_land_caps_df.rename(columns={"land_cap_mw": "zone_land_cap_mw"}).copy()
    anchors = anchors.merge(land_caps[[zone_col, "zone_land_cap_mw"]], on=zone_col, how="left")
    anchors["zone_land_cap_mw"] = pd.to_numeric(anchors["zone_land_cap_mw"], errors="coerce").fillna(0.0)
    eligible = anchors[anchors["zone_land_cap_mw"] > 0].copy()

    zones_with_land = set(land_caps[land_caps["zone_land_cap_mw"] > 0][zone_col].astype(str))
    zones_with_anchors = set(eligible[zone_col].astype(str))
    unallocated = pd.DataFrame(
        {
            zone_col: sorted(zones_with_land - zones_with_anchors),
            "unallocated_zone": True,
        }
    )

    if eligible.empty:
        return eligible, unallocated

    zone_scores = (
        eligible.groupby(zone_col, dropna=False)["demand_score"]
        .sum()
        .reset_index(name="zone_demand_score")
    )
    eligible = eligible.merge(zone_scores, on=zone_col, how="left")

    total_score = float(zone_scores["zone_demand_score"].sum())
    if total_score > 0:
        zone_scores["zone_fes_share_mw"] = (
            national_fes_total_mw * zone_scores["zone_demand_score"] / total_score
        )
    else:
        zone_scores["zone_fes_share_mw"] = 0.0

    eligible = eligible.merge(zone_scores[[zone_col, "zone_fes_share_mw"]], on=zone_col, how="left")
    eligible["anchor_fes_share_mw"] = _weighted_zone_shares(
        eligible,
        value_col="zone_fes_share_mw",
        zone_col=zone_col,
        score_col="demand_score",
    )
    eligible["anchor_land_cap_mw"] = _weighted_zone_shares(
        eligible,
        value_col="zone_land_cap_mw",
        zone_col=zone_col,
        score_col="demand_score",
    )
    eligible["live_existing_capacity_mw"] = 0.0
    eligible["local_headroom_mw"] = eligible["anchor_land_cap_mw"]
    eligible["oversubscribed"] = False
    eligible["oversubscription_amount_mw"] = 0.0

    return eligible.reset_index(drop=True), unallocated.reset_index(drop=True)


def build_large_nuclear_candidate_rows(
    headroom_df: pd.DataFrame,
    modelled_year: int,
    capital_cost: float,
) -> pd.DataFrame:
    """Create extendable large-nuclear candidate rows."""
    candidates = headroom_df[headroom_df["local_headroom_mw"] > 0].copy()
    rows = []
    for _, row in candidates.iterrows():
        site_id = str(row["site_id"])
        rows.append(
            {
                "station_name": f"FESCandidate_nuclear_{site_id}_{modelled_year}",
                "capacity_mw": 0.0,
                "fuel_type": "nuclear",
                "bus": str(row["bus"]),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "data_source": "FES_candidate",
                "p_nom_extendable": True,
                "p_nom_min": 0.0,
                "p_nom_max": float(row["local_headroom_mw"]),
                "capital_cost": float(capital_cost),
                "future_candidate_group": "large_nuclear",
                "future_candidate_site_id": site_id,
                "future_candidate_label": str(row["site_name"]),
                "future_candidate_country": str(row.get("country", "")),
                "future_candidate_local_cap_mw": float(row["local_siting_cap_mw"]),
            }
        )
    return pd.DataFrame.from_records(rows)


def build_smr_candidate_rows(
    headroom_df: pd.DataFrame,
    modelled_year: int,
    capital_cost: float,
) -> pd.DataFrame:
    """Create extendable SMR candidate rows."""
    candidates = headroom_df[headroom_df["anchor_fes_share_mw"] > 0].copy()
    rows = []
    for _, row in candidates.iterrows():
        anchor_id = str(row["anchor_id"])
        rows.append(
            {
                "station_name": f"FESCandidate_smr_{anchor_id}_{modelled_year}",
                "capacity_mw": 0.0,
                "fuel_type": "smr",
                "bus": str(row["bus"]),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "data_source": "FES_candidate",
                "p_nom_extendable": True,
                "p_nom_min": 0.0,
                "p_nom_max": float(row["anchor_fes_share_mw"]),
                "capital_cost": float(capital_cost),
                "future_candidate_group": "smr",
                "future_candidate_anchor_id": anchor_id,
                "future_candidate_label": str(row["anchor_name"]),
                "future_candidate_anchor_type": str(row["anchor_type"]),
                "future_candidate_land_cap_mw": float(row["anchor_land_cap_mw"]),
            }
        )
    return pd.DataFrame.from_records(rows)
