"""
Compare baseline and land-constrained solved networks.

This helper turns the manual A/B checklist for technical potential constraints
into a repeatable CLI report. It is intended for paired scenario runs such as:

  - HT35_zonal
  - HT35_zonal_constrained

Usage:
  python scripts/analysis/compare_land_constraint_runs.py ^
    --baseline resources/network/HT35_zonal_solved.nc ^
    --constrained resources/network/HT35_zonal_constrained_solved.nc ^
    --report resources/generators/HT35_zonal_constrained_constrained_report.txt
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


EPSILON = 1e-6
LOAD_SHEDDING_PATTERN = "load_shedding|load shedding|voll"


def component_counts(network) -> dict[str, int]:
    """Return high-level component counts for parity checks."""
    return {
        "buses": len(network.buses),
        "generators": len(network.generators),
        "storage_units": len(network.storage_units),
        "links": len(network.links),
        "stores": len(network.stores),
        "lines": len(network.lines),
    }


def capacity_table(network) -> pd.DataFrame:
    """Aggregate generator capacities by carrier and bus."""
    generators = network.generators.copy()

    for column in ("p_nom", "p_nom_max", "p_nom_opt"):
        if column not in generators.columns:
            generators[column] = 0.0

    return (
        generators[["carrier", "bus", "p_nom", "p_nom_max", "p_nom_opt"]]
        .groupby(["carrier", "bus"], dropna=False)[["p_nom", "p_nom_max", "p_nom_opt"]]
        .sum()
        .sort_index()
    )


def dispatch_table(network) -> pd.DataFrame:
    """Aggregate solved generation by carrier and bus."""
    generators = network.generators[["carrier", "bus"]].copy()
    generators["generation_MWh"] = network.generators_t.p.sum().reindex(generators.index).fillna(0.0)

    return (
        generators.groupby(["carrier", "bus"], dropna=False)[["generation_MWh"]]
        .sum()
        .sort_index()
    )


def load_shedding_total(network) -> float:
    """Return total load shedding across all matching generators."""
    mask = network.generators["carrier"].astype(str).str.lower().str.contains(LOAD_SHEDDING_PATTERN, regex=True)
    matching_generators = network.generators.index[mask]
    if len(matching_generators) == 0:
        return 0.0

    matching_columns = [generator for generator in matching_generators if generator in network.generators_t.p.columns]
    if not matching_columns:
        return 0.0

    return float(network.generators_t.p[matching_columns].sum().sum())


def summarize_diff(diff: pd.DataFrame, value_columns: list[str]) -> pd.DataFrame:
    """Keep only rows with material differences."""
    if diff.empty:
        return diff

    row_mask = diff[value_columns].abs().sum(axis=1) > EPSILON
    return diff.loc[row_mask].sort_index()


def format_component_parity(base_counts: dict[str, int], constrained_counts: dict[str, int]) -> list[str]:
    """Render component parity section and warnings."""
    lines = [
        "Component counts:",
        f"  baseline    {base_counts}",
        f"  constrained {constrained_counts}",
    ]

    parity_issues = []
    for component in ("storage_units", "links", "stores", "lines"):
        if base_counts.get(component) != constrained_counts.get(component):
            parity_issues.append(component)

    if parity_issues:
        lines.append("")
        lines.append("PARITY WARNING:")
        lines.append(
            "  Downstream component families differ between solved networks: "
            + ", ".join(parity_issues)
        )
        lines.append(
            "  Treat dispatch/cost differences as workflow-wiring-sensitive until branch parity is confirmed."
        )
    else:
        lines.append("  Parity gate: PASS")

    return lines


def parse_constraint_report(report_path: Path) -> list[str]:
    """Extract the most useful lines from the technical-potential report."""
    if not report_path.exists():
        return [f"Constraint report not found: {report_path}"]

    lines = report_path.read_text(encoding="utf-8").splitlines()
    keep_prefixes = (
        "Status:",
        "Reason:",
        "Technical Potential CSV:",
        "Constraints from CSV:",
        "Generators constrained:",
        "Constraints applied:",
        "Binding constraints:",
        "Technology exclusions:",
        "Unmatched constraint entries:",
    )

    extracted = [line.strip() for line in lines if line.strip().startswith(keep_prefixes)]
    if not extracted:
        return [f"Constraint report present but no summary lines matched: {report_path}"]

    return extracted


def load_location_constraint_report(report_path: Path) -> pd.DataFrame:
    """Load a post-policy location-constraint report if available."""
    if not report_path.exists():
        return pd.DataFrame()
    return pd.read_csv(report_path)


def summarize_location_constraint_reports(
    baseline_report_path: Path,
    constrained_report_path: Path,
) -> list[str]:
    """Summarize how post-policy candidate rows differ between scenarios."""
    baseline_df = load_location_constraint_report(baseline_report_path)
    constrained_df = load_location_constraint_report(constrained_report_path)

    if baseline_df.empty or constrained_df.empty:
        return [
            "Location-constraint report summary unavailable:",
            f"  baseline report rows: {len(baseline_df)}",
            f"  constrained report rows: {len(constrained_df)}",
        ]

    merged = baseline_df[
        ["candidate_name", "carrier", "bus", "p_nom_max_post_policy_mw", "p_nom_opt_mw"]
    ].rename(
        columns={
            "p_nom_max_post_policy_mw": "baseline_p_nom_max_post_policy_mw",
            "p_nom_opt_mw": "baseline_p_nom_opt_mw",
        }
    ).merge(
        constrained_df[
            [
                "candidate_name",
                "carrier",
                "bus",
                "p_nom_max_post_policy_mw",
                "p_nom_opt_mw",
                "constraint_outcome",
            ]
        ].rename(
            columns={
                "p_nom_max_post_policy_mw": "constrained_p_nom_max_post_policy_mw",
                "p_nom_opt_mw": "constrained_p_nom_opt_mw",
                "constraint_outcome": "constrained_constraint_outcome",
            }
        ),
        on=["candidate_name", "carrier", "bus"],
        how="outer",
    )

    for column in (
        "baseline_p_nom_max_post_policy_mw",
        "baseline_p_nom_opt_mw",
        "constrained_p_nom_max_post_policy_mw",
        "constrained_p_nom_opt_mw",
    ):
        merged[column] = pd.to_numeric(merged[column], errors="coerce").fillna(0.0)

    changed_post_policy = merged[
        (merged["constrained_p_nom_max_post_policy_mw"] - merged["baseline_p_nom_max_post_policy_mw"]).abs() > EPSILON
    ]
    changed_p_nom_opt = merged[
        (merged["constrained_p_nom_opt_mw"] - merged["baseline_p_nom_opt_mw"]).abs() > EPSILON
    ]
    oversubscribed_blocked = constrained_df[
        constrained_df["constraint_outcome"].astype(str).eq("oversubscribed_blocked")
    ]
    tightened_and_built = constrained_df[
        constrained_df["constraint_outcome"].astype(str).eq("tightened_and_built")
    ]

    def _example_list(df: pd.DataFrame) -> str:
        if df.empty:
            return "none"
        return ", ".join(df["candidate_name"].astype(str).head(5).tolist())

    return [
        "Location-constraint report summary:",
        f"  baseline report rows: {len(baseline_df)}",
        f"  constrained report rows: {len(constrained_df)}",
        f"  rows with changed post-policy p_nom_max: {len(changed_post_policy)}",
        f"    examples: {_example_list(changed_post_policy)}",
        f"  rows with changed p_nom_opt: {len(changed_p_nom_opt)}",
        f"    examples: {_example_list(changed_p_nom_opt)}",
        f"  constrained rows classified as oversubscribed_blocked: {len(oversubscribed_blocked)}",
        f"    examples: {_example_list(oversubscribed_blocked)}",
        f"  constrained rows classified as tightened_and_built: {len(tightened_and_built)}",
        f"    examples: {_example_list(tightened_and_built)}",
    ]


def build_report(
    baseline_path: Path,
    constrained_path: Path,
    constraint_report: Path | None,
    baseline_location_report: Path | None = None,
    constrained_location_report: Path | None = None,
) -> str:
    """Produce the full comparison report."""
    from scripts.utilities.network_io import load_network

    baseline = load_network(baseline_path)
    constrained = load_network(constrained_path)

    baseline_counts = component_counts(baseline)
    constrained_counts = component_counts(constrained)

    capacity_diff = summarize_diff(
        capacity_table(constrained).subtract(capacity_table(baseline), fill_value=0.0),
        ["p_nom", "p_nom_max", "p_nom_opt"],
    )
    dispatch_diff = summarize_diff(
        dispatch_table(constrained).subtract(dispatch_table(baseline), fill_value=0.0),
        ["generation_MWh"],
    )

    lines = [
        "LAND-CONSTRAINT A/B COMPARISON",
        "=" * 80,
        f"Baseline network:    {baseline_path}",
        f"Constrained network: {constrained_path}",
        "",
    ]
    lines.extend(format_component_parity(baseline_counts, constrained_counts))
    lines.extend(
        [
            "",
            "Objective:",
            f"  baseline    {getattr(baseline, 'objective', None)}",
            f"  constrained {getattr(constrained, 'objective', None)}",
            "",
            "Load shedding MWh:",
            f"  baseline    {load_shedding_total(baseline):,.3f}",
            f"  constrained {load_shedding_total(constrained):,.3f}",
        ]
    )

    if constraint_report is not None:
        lines.extend(["", "Constraint application report:"])
        lines.extend(f"  {line}" for line in parse_constraint_report(constraint_report))

    if baseline_location_report is not None and constrained_location_report is not None:
        lines.extend([""])
        lines.extend(
            summarize_location_constraint_reports(
                baseline_report_path=baseline_location_report,
                constrained_report_path=constrained_location_report,
            )
        )

    lines.extend(["", "Capacity differences by carrier/bus:"])
    if capacity_diff.empty:
        lines.append("  No material differences found.")
    else:
        lines.append(capacity_diff.to_string())

    lines.extend(["", "Dispatch differences by carrier/bus (MWh):"])
    if dispatch_diff.empty:
        lines.append("  No material differences found.")
    else:
        lines.append(dispatch_diff.to_string())

    return "\n".join(lines)


def main() -> None:
    """Parse arguments and print the A/B comparison report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Path to the baseline solved network (.nc)")
    parser.add_argument("--constrained", required=True, help="Path to the constrained solved network (.nc)")
    parser.add_argument(
        "--report",
        help="Optional path to the constrained technical-potential report "
        "(for example resources/generators/HT35_zonal_constrained_constrained_report.txt)",
    )
    parser.add_argument(
        "--output",
        help="Optional path to save the comparison report as plain text.",
    )
    parser.add_argument(
        "--baseline-location-report",
        help="Optional path to the baseline post-policy location-constraint report CSV.",
    )
    parser.add_argument(
        "--constrained-location-report",
        help="Optional path to the constrained post-policy location-constraint report CSV.",
    )
    args = parser.parse_args()

    report_text = build_report(
        baseline_path=Path(args.baseline),
        constrained_path=Path(args.constrained),
        constraint_report=Path(args.report) if args.report else None,
        baseline_location_report=Path(args.baseline_location_report) if args.baseline_location_report else None,
        constrained_location_report=Path(args.constrained_location_report) if args.constrained_location_report else None,
    )

    print(report_text)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text, encoding="utf-8")


if __name__ == "__main__":
    main()
