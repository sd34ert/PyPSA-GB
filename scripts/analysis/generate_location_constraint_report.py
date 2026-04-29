"""
Generate a post-policy, solved-aware location-constraint report.

This report explains candidate siting/cap behavior row by row by joining:
  - the pre-policy future-capacity oversubscription report
  - the assembled pre-policy network
  - the finalized post-policy network
  - the solved network
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.utilities.logging_config import setup_logging
from scripts.utilities.network_io import load_network


FES_CANDIDATE_PREFIX = "FESCandidate_"
EPSILON = 1e-6
REPORT_COLUMNS = [
    "candidate_name",
    "carrier",
    "candidate_group",
    "zone_name",
    "bus",
    "site_or_anchor_id",
    "site_or_anchor_name",
    "anchor_type",
    "fes_spatial_cap_mw",
    "land_cap_mw",
    "row_level_land_cap_mw",
    "effective_total_cap_mw",
    "live_existing_capacity_mw",
    "extendable_headroom_pre_land_mw",
    "extendable_headroom_mw",
    "land_binding",
    "existing_oversubscribed",
    "possible_preallocation_artifact",
    "oversubscribed",
    "oversubscription_amount_mw",
    "pre_policy_p_nom_max_mw",
    "post_policy_p_nom_max_mw",
    "p_nom_max_pre_policy_mw",
    "p_nom_max_post_policy_mw",
    "p_nom_opt_mw",
    "policy_headroom_removed_mw",
    "policy_constraint_binding",
    "constraint_outcome",
]


def empty_location_constraint_report() -> pd.DataFrame:
    """Return an empty location-constraint report with the standard schema."""
    return pd.DataFrame(columns=REPORT_COLUMNS)


def _candidate_mask(index: pd.Index) -> pd.Series:
    return index.to_series().astype(str).str.startswith(FES_CANDIDATE_PREFIX)


def _string_key(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str)


def _numeric(series: pd.Series | None, default: float = 0.0) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series, errors="coerce").fillna(default)


def _bool_series(series: pd.Series | None, default: bool = False) -> pd.Series:
    """Return a nullable input series as plain booleans."""
    if series is None:
        return pd.Series(dtype=bool)
    if series.dtype == bool:
        return series.fillna(default).astype(bool)
    return (
        series.fillna(default)
        .astype(str)
        .str.strip()
        .str.casefold()
        .map({"true": True, "false": False, "1": True, "0": False})
        .fillna(default)
        .astype(bool)
    )


def candidate_table(network, value_column: str) -> pd.DataFrame:
    """Extract one row per future candidate generator from a network."""
    generators = network.generators.copy()
    candidates = generators.loc[_candidate_mask(generators.index)].copy()
    if candidates.empty:
        return pd.DataFrame(
            columns=[
                "candidate_name",
                "carrier",
                "bus",
                "candidate_group",
                "site_or_anchor_id",
                "site_or_anchor_name",
                value_column,
            ]
        )

    output = pd.DataFrame(index=candidates.index)
    output["candidate_name"] = candidates.index.astype(str)
    output["carrier"] = _string_key(candidates["carrier"])
    output["bus"] = _string_key(candidates["bus"])
    output["candidate_group"] = _string_key(
        candidates["future_candidate_group"]
        if "future_candidate_group" in candidates.columns
        else pd.Series("", index=candidates.index)
    )
    output["site_or_anchor_id"] = _string_key(
        candidates["future_candidate_site_id"]
        if "future_candidate_site_id" in candidates.columns
        else pd.Series("", index=candidates.index)
    )
    output["site_or_anchor_name"] = _string_key(
        candidates["future_candidate_label"]
        if "future_candidate_label" in candidates.columns
        else pd.Series("", index=candidates.index)
    )
    output[value_column] = _numeric(
        candidates[value_column] if value_column in candidates.columns else pd.Series(np.nan, index=candidates.index)
    )
    return output.reset_index(drop=True)


def load_pre_policy_report(report_path: str | Path) -> pd.DataFrame:
    """Load the pre-policy future-capacity report and normalize join columns."""
    path = Path(report_path)
    if not path.exists():
        return empty_location_constraint_report()

    df = pd.read_csv(path)
    if df.empty:
        return empty_location_constraint_report()

    optional_defaults = {
        "candidate_group": "",
        "zone_name": "",
        "bus": "",
        "site_or_anchor_id": "",
        "site_or_anchor_name": "",
        "anchor_type": "",
        "fes_spatial_cap_mw": np.nan,
        "land_cap_mw": np.nan,
        "row_level_land_cap_mw": np.nan,
        "effective_total_cap_mw": np.nan,
        "live_existing_capacity_mw": np.nan,
        "extendable_headroom_pre_land_mw": np.nan,
        "extendable_headroom_mw": np.nan,
        "land_binding": False,
        "existing_oversubscribed": False,
        "possible_preallocation_artifact": False,
        "oversubscribed": False,
        "oversubscription_amount_mw": 0.0,
    }
    for column, default in optional_defaults.items():
        if column not in df.columns:
            df[column] = default

    df["carrier"] = _string_key(df["carrier"])
    df["bus"] = _string_key(df["bus"])
    df["candidate_group"] = _string_key(df["candidate_group"])
    df["site_or_anchor_id"] = _string_key(df["site_or_anchor_id"])
    df["site_or_anchor_name"] = _string_key(df["site_or_anchor_name"])
    df["anchor_type"] = _string_key(df["anchor_type"])
    df["zone_name"] = _string_key(df["zone_name"])
    for column in (
        "oversubscribed",
        "land_binding",
        "existing_oversubscribed",
        "possible_preallocation_artifact",
    ):
        df[column] = _bool_series(df[column], default=False)
    return df


def classify_constraint_outcome(row: pd.Series) -> str:
    """Assign a compact interpretation label for each candidate row."""
    post_policy = float(row.get("p_nom_max_post_policy_mw", 0.0) or 0.0)
    policy_binding = bool(row.get("policy_constraint_binding", False))
    p_nom_opt = float(row.get("p_nom_opt_mw", 0.0) or 0.0)
    oversubscribed = bool(row.get("oversubscribed", False))

    if oversubscribed and post_policy <= EPSILON:
        return "oversubscribed_blocked"
    if policy_binding and p_nom_opt > EPSILON:
        return "tightened_and_built"
    if policy_binding:
        return "tightened_nonbinding"
    if p_nom_opt <= EPSILON:
        return "no_candidate_build"
    return "unchanged"


def build_location_constraint_report(
    *,
    pre_policy_network,
    post_policy_network,
    solved_network,
    pre_policy_report_df: pd.DataFrame,
) -> pd.DataFrame:
    """Build the combined location-constraint report from in-memory objects."""
    pre_policy_candidates = candidate_table(pre_policy_network, "p_nom_max")
    if pre_policy_candidates.empty:
        return empty_location_constraint_report()

    pre_policy_report_df = pre_policy_report_df.copy()
    for column, default in {
        "land_binding": False,
        "existing_oversubscribed": False,
        "possible_preallocation_artifact": False,
        "oversubscribed": False,
    }.items():
        if column not in pre_policy_report_df.columns:
            pre_policy_report_df[column] = default
        pre_policy_report_df[column] = _bool_series(pre_policy_report_df[column], default=False)

    for column, default in {
        "fes_spatial_cap_mw": np.nan,
        "land_cap_mw": np.nan,
        "row_level_land_cap_mw": np.nan,
        "effective_total_cap_mw": np.nan,
        "live_existing_capacity_mw": np.nan,
        "extendable_headroom_pre_land_mw": np.nan,
        "extendable_headroom_mw": np.nan,
        "oversubscription_amount_mw": 0.0,
        "candidate_group": "",
        "zone_name": "",
        "site_or_anchor_id": "",
        "site_or_anchor_name": "",
        "anchor_type": "",
    }.items():
        if column not in pre_policy_report_df.columns:
            pre_policy_report_df[column] = default

    pre_policy_candidates = pre_policy_candidates.rename(columns={"p_nom_max": "p_nom_max_pre_policy_mw"})
    post_policy_candidates = candidate_table(post_policy_network, "p_nom_max").rename(
        columns={"p_nom_max": "p_nom_max_post_policy_mw"}
    )
    solved_candidates = candidate_table(solved_network, "p_nom_opt").rename(
        columns={"p_nom_opt": "p_nom_opt_mw"}
    )

    report = pre_policy_candidates.merge(
        pre_policy_report_df[
            [
                "carrier",
                "candidate_group",
                "zone_name",
                "bus",
                "site_or_anchor_id",
                "site_or_anchor_name",
                "anchor_type",
                "fes_spatial_cap_mw",
                "land_cap_mw",
                "row_level_land_cap_mw",
                "effective_total_cap_mw",
                "live_existing_capacity_mw",
                "extendable_headroom_pre_land_mw",
                "extendable_headroom_mw",
                "land_binding",
                "existing_oversubscribed",
                "possible_preallocation_artifact",
                "oversubscribed",
                "oversubscription_amount_mw",
            ]
        ],
        on=["carrier", "candidate_group", "bus", "site_or_anchor_id"],
        how="left",
    )

    report["site_or_anchor_name"] = report["site_or_anchor_name_x"].where(
        report["site_or_anchor_name_x"].astype(str).str.len() > 0,
        report["site_or_anchor_name_y"],
    )
    report = report.drop(columns=["site_or_anchor_name_x", "site_or_anchor_name_y"])

    report = report.merge(
        post_policy_candidates[["candidate_name", "p_nom_max_post_policy_mw"]],
        on="candidate_name",
        how="left",
    )
    report = report.merge(
        solved_candidates[["candidate_name", "p_nom_opt_mw"]],
        on="candidate_name",
        how="left",
    )

    for column in (
        "p_nom_max_pre_policy_mw",
        "p_nom_max_post_policy_mw",
        "p_nom_opt_mw",
        "fes_spatial_cap_mw",
        "land_cap_mw",
        "row_level_land_cap_mw",
        "effective_total_cap_mw",
        "live_existing_capacity_mw",
        "extendable_headroom_pre_land_mw",
        "extendable_headroom_mw",
        "oversubscription_amount_mw",
    ):
        report[column] = _numeric(report[column], default=0.0)

    report["zone_name"] = report["zone_name"].where(report["zone_name"].astype(str).str.len() > 0, report["bus"])
    report["p_nom_max_post_policy_mw"] = report["p_nom_max_post_policy_mw"].fillna(
        report["p_nom_max_pre_policy_mw"]
    )
    report["p_nom_opt_mw"] = report["p_nom_opt_mw"].fillna(0.0)
    report["oversubscribed"] = report["oversubscribed"].fillna(False).astype(bool)
    land_binding_from_report = _bool_series(report["land_binding"], default=False)
    existing_oversubscribed_from_report = _bool_series(
        report["existing_oversubscribed"],
        default=False,
    )
    possible_preallocation_from_report = _bool_series(
        report["possible_preallocation_artifact"],
        default=False,
    )
    report["land_binding"] = (
        land_binding_from_report
        | (
            (report["land_cap_mw"] > 0)
            & (report["land_cap_mw"] < (report["fes_spatial_cap_mw"] - EPSILON))
        )
    )
    report["existing_oversubscribed"] = (
        existing_oversubscribed_from_report
        | report["oversubscribed"]
        | (report["live_existing_capacity_mw"] > (report["effective_total_cap_mw"] + EPSILON))
    )
    report["policy_headroom_removed_mw"] = np.maximum(
        report["p_nom_max_pre_policy_mw"] - report["p_nom_max_post_policy_mw"],
        0.0,
    )
    report["policy_constraint_binding"] = (
        report["p_nom_max_post_policy_mw"] < (report["p_nom_max_pre_policy_mw"] - EPSILON)
    )
    report["possible_preallocation_artifact"] = (
        possible_preallocation_from_report
        | (
            report["land_binding"]
            & report["existing_oversubscribed"]
            & (report["extendable_headroom_pre_land_mw"] > EPSILON)
            & (report["p_nom_max_post_policy_mw"] <= EPSILON)
        )
    )
    report["pre_policy_p_nom_max_mw"] = report["p_nom_max_pre_policy_mw"]
    report["post_policy_p_nom_max_mw"] = report["p_nom_max_post_policy_mw"]
    report["constraint_outcome"] = report.apply(classify_constraint_outcome, axis=1)

    for column in ("candidate_name", "carrier", "candidate_group", "zone_name", "bus", "site_or_anchor_id", "site_or_anchor_name", "anchor_type"):
        report[column] = _string_key(report[column])

    report = report[REPORT_COLUMNS].sort_values(
        ["carrier", "zone_name", "candidate_group", "candidate_name"]
    ).reset_index(drop=True)
    return report


def build_location_constraint_report_from_paths(
    *,
    pre_policy_network_path: str | Path,
    post_policy_network_path: str | Path,
    solved_network_path: str | Path,
    pre_policy_report_path: str | Path,
) -> pd.DataFrame:
    """Load inputs from disk and build the report."""
    return build_location_constraint_report(
        pre_policy_network=load_network(pre_policy_network_path),
        post_policy_network=load_network(post_policy_network_path),
        solved_network=load_network(solved_network_path),
        pre_policy_report_df=load_pre_policy_report(pre_policy_report_path),
    )


def write_location_constraint_report(report_df: pd.DataFrame, output_path: str | Path) -> None:
    """Write a schema-stable CSV location-constraint report."""
    output_df = report_df.copy() if report_df is not None else empty_location_constraint_report()
    if output_df.empty:
        output_df = empty_location_constraint_report()
    else:
        for column in REPORT_COLUMNS:
            if column not in output_df.columns:
                output_df[column] = np.nan
        output_df = output_df[REPORT_COLUMNS]
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_df.to_csv(output_path, index=False)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre-policy-network", required=True)
    parser.add_argument("--post-policy-network", required=True)
    parser.add_argument("--solved-network", required=True)
    parser.add_argument("--pre-policy-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--log", default="logs/analysis/generate_location_constraint_report.log")
    return parser


def _run(pre_policy_network_path: str, post_policy_network_path: str, solved_network_path: str, pre_policy_report_path: str, output_path: str, log_path: str) -> None:
    logger = setup_logging(log_path)
    logger.info("Generating location-constraint report")
    report = build_location_constraint_report_from_paths(
        pre_policy_network_path=pre_policy_network_path,
        post_policy_network_path=post_policy_network_path,
        solved_network_path=solved_network_path,
        pre_policy_report_path=pre_policy_report_path,
    )
    write_location_constraint_report(report, output_path)
    logger.info("Wrote location-constraint report to %s", output_path)


def main() -> None:
    if "snakemake" in globals():
        _run(
            pre_policy_network_path=str(snakemake.input.pre_policy_network),
            post_policy_network_path=str(snakemake.input.post_policy_network),
            solved_network_path=str(snakemake.input.solved_network),
            pre_policy_report_path=str(snakemake.input.pre_policy_report),
            output_path=str(snakemake.output.report),
            log_path=str(snakemake.log[0]) if snakemake.log else "logs/analysis/generate_location_constraint_report.log",
        )
        return

    args = _build_arg_parser().parse_args()
    _run(
        pre_policy_network_path=args.pre_policy_network,
        post_policy_network_path=args.post_policy_network,
        solved_network_path=args.solved_network,
        pre_policy_report_path=args.pre_policy_report,
        output_path=args.output,
        log_path=args.log,
    )


if __name__ == "__main__":
    main()
