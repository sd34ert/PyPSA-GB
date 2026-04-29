"""
Apply technical-potential constraints to the fully assembled pre-solve network.

This policy-layer step now prefers extendable FES candidate generators created by
the future-capacity-candidate workflow. Fixed baseline generators remain
grandfathered; land caps tighten new-build headroom rather than forcing existing
capacity down. If no candidates exist for a carrier-zone pair, the legacy fixed
generator fallback is still used.
"""

import logging
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from scripts.generators.future_capacity_candidates import build_land_cap_table


FES_CANDIDATE_PREFIX = "FESCandidate_"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_network(network_path):
    """Load a PyPSA network from pickle or NetCDF."""
    logger.info(f"Loading network from {network_path}")
    if str(network_path).endswith(".pkl"):
        with open(network_path, "rb") as handle:
            return pickle.load(handle)
    return pypsa.Network(network_path)


def load_technical_potential(csv_path):
    """Load the raw technical-potential CSV for reporting and carrier mapping."""
    logger.info(f"Loading technical potential from {csv_path}")
    df = pd.read_csv(csv_path)
    required_cols = ["zone_name", "carrier", "p_nom_max_mw"]
    missing = [column for column in required_cols if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in technical_potential CSV: {missing}")

    logger.info(f"Loaded technical potential for {len(df)} carrier-zone combinations")
    logger.info(f"Carriers: {sorted(df['carrier'].unique())}")
    logger.info(f"Zones: {sorted(df['zone_name'].unique())}")
    return df


def extract_zone_from_bus(bus_name, network):
    """Extract the zone identifier used by the land-cap tables."""
    if "zone" in network.buses.columns and bus_name in network.buses.index:
        zone = network.buses.loc[bus_name, "zone"]
        if pd.notna(zone):
            return zone

    if bus_name in [
        "Z1_1",
        "Z1_2",
        "Z1_3",
        "Z1_4",
        "Z2",
        "Z3",
        "Z4",
        "Z5",
        "Z6",
        "Z7",
        "Z8",
        "Z9",
        "Z10",
        "Z11",
        "Z12",
        "Z13",
        "Z14",
        "Z15",
        "Z16",
        "Z17",
    ]:
        return bus_name

    if bus_name in ["DOGGER_BANK", "HORNSEA"]:
        return bus_name

    return None


def _candidate_mask(generators: pd.DataFrame) -> pd.Series:
    return generators.index.to_series().astype(str).str.startswith(FES_CANDIDATE_PREFIX)


def _distribute_headroom(existing_limits: pd.Series, total_headroom: float) -> pd.Series:
    """
    Distribute a total land-adjusted headroom across one or more candidate rows.

    In the current workflow there should only be one candidate per (carrier, bus),
    but this keeps the rule safe if that changes later.
    """
    total_headroom = float(max(total_headroom, 0.0))
    if len(existing_limits) == 0:
        return existing_limits.copy()

    finite_limits = pd.to_numeric(existing_limits, errors="coerce").fillna(0.0)
    total_existing_limit = finite_limits.sum()
    if total_existing_limit > 0:
        shares = finite_limits / total_existing_limit
    else:
        shares = pd.Series(1.0 / len(existing_limits), index=existing_limits.index)

    allocated = shares * total_headroom
    return pd.concat([finite_limits, allocated], axis=1).min(axis=1)


def apply_technical_potential_constraints(network, technical_potential_df):
    """Apply land-derived caps to future candidates first, then legacy fixed generators."""
    logger.info("\n" + "=" * 80)
    logger.info("APPLYING TECHNICAL POTENTIAL CONSTRAINTS")
    logger.info("=" * 80)

    generators = network.generators.copy()
    generators["zone_name"] = generators["bus"].map(lambda bus: extract_zone_from_bus(bus, network))
    generators["is_candidate"] = _candidate_mask(generators)
    generators["p_nom_extendable"] = generators.get("p_nom_extendable", False)
    custom_land_cap_source = (
        generators["future_candidate_land_cap_mw"]
        if "future_candidate_land_cap_mw" in generators.columns
        else pd.Series(np.nan, index=generators.index)
    )
    generators["future_candidate_land_cap_mw"] = pd.to_numeric(
        custom_land_cap_source,
        errors="coerce",
    )
    generators["has_custom_land_cap"] = generators["future_candidate_land_cap_mw"].notna()

    raw_constraint_entries = len(
        technical_potential_df[["carrier", "zone_name"]].drop_duplicates()
    )
    land_caps = build_land_cap_table(technical_potential_df)

    stats = {
        "constraints_from_csv": raw_constraint_entries,
        "mapped_constraint_rows": len(land_caps),
        "generators_affected": 0,
        "constraints_applied": 0,
        "constraints_binding": 0,
        "zero_constraints": int((land_caps["land_cap_mw"] <= 0).sum()) if len(land_caps) > 0 else 0,
        "no_matching_gens": 0,
        "fixed_baseline_matches": 0,
        "extendable_candidates_matched": 0,
        "candidate_constraints_tightened": 0,
        "candidate_oversubscribed_rows": 0,
        "possible_preallocation_artifact_rows": 0,
        "row_level_candidate_caps_applied": 0,
        "carrier_zone_mismatches": [],
    }

    custom_candidate_rows = generators.index[generators["is_candidate"] & generators["has_custom_land_cap"]]
    for gen_idx in custom_candidate_rows:
        old_p_nom_max = pd.to_numeric(generators.loc[gen_idx, "p_nom_max"], errors="coerce")
        old_p_nom_max = float(old_p_nom_max) if pd.notna(old_p_nom_max) else np.inf
        custom_land_cap = float(generators.loc[gen_idx, "future_candidate_land_cap_mw"])
        new_p_nom_max = min(old_p_nom_max, custom_land_cap)

        network.generators.loc[gen_idx, "pre_policy_p_nom_max_mw"] = old_p_nom_max
        network.generators.loc[gen_idx, "policy_land_cap_mw"] = custom_land_cap
        network.generators.loc[gen_idx, "policy_live_existing_capacity_mw"] = 0.0
        network.generators.loc[gen_idx, "policy_effective_headroom_mw"] = new_p_nom_max
        network.generators.loc[gen_idx, "policy_existing_oversubscribed"] = False
        network.generators.loc[gen_idx, "policy_possible_preallocation_artifact"] = False
        network.generators.loc[gen_idx, "p_nom_max"] = new_p_nom_max
        network.generators.loc[gen_idx, "post_policy_p_nom_max_mw"] = new_p_nom_max
        generators.loc[gen_idx, "p_nom_max"] = new_p_nom_max
        stats["constraints_applied"] += 1
        stats["generators_affected"] += 1
        stats["extendable_candidates_matched"] += 1
        stats["row_level_candidate_caps_applied"] += 1
        if not np.isclose(old_p_nom_max, new_p_nom_max):
            stats["constraints_binding"] += 1
            stats["candidate_constraints_tightened"] += 1

        logger.info(
            "✓ %s: row-level candidate land cap %.0f MW applied (old p_nom_max %.0f MW -> %.0f MW)",
            gen_idx,
            custom_land_cap,
            old_p_nom_max,
            new_p_nom_max,
        )

    for _, row in land_caps.iterrows():
        carrier = row["carrier"]
        zone_name = row["zone_name"]
        land_cap_mw = float(row["land_cap_mw"])

        zone_mask = (
            generators["carrier"].astype(str).eq(str(carrier))
            & generators["zone_name"].astype(str).eq(str(zone_name))
        )
        candidate_mask = zone_mask & generators["is_candidate"] & ~generators["has_custom_land_cap"]
        fixed_mask = zone_mask & ~generators["is_candidate"]

        matching_candidates = generators.index[candidate_mask]
        matching_fixed = generators.index[fixed_mask]

        if len(matching_candidates) > 0:
            live_existing_capacity = float(
                pd.to_numeric(generators.loc[matching_fixed, "p_nom"], errors="coerce")
                .fillna(0.0)
                .sum()
            )
            effective_headroom = max(land_cap_mw - live_existing_capacity, 0.0)

            if live_existing_capacity > land_cap_mw:
                stats["candidate_oversubscribed_rows"] += 1

            current_limits = pd.to_numeric(
                generators.loc[matching_candidates, "p_nom_max"], errors="coerce"
            ).fillna(0.0)
            new_limits = _distribute_headroom(current_limits, effective_headroom)

            for gen_idx in matching_candidates:
                old_p_nom_max = float(current_limits.loc[gen_idx])
                new_p_nom_max = float(new_limits.loc[gen_idx])
                estimated_fes_spatial_cap = old_p_nom_max + live_existing_capacity
                land_binding = land_cap_mw < (estimated_fes_spatial_cap - 1e-9)
                existing_oversubscribed = live_existing_capacity > (land_cap_mw + 1e-9)
                possible_preallocation_artifact = (
                    land_binding
                    and existing_oversubscribed
                    and old_p_nom_max > 1e-9
                    and new_p_nom_max <= 1e-9
                )
                if possible_preallocation_artifact:
                    stats["possible_preallocation_artifact_rows"] += 1
                network.generators.loc[gen_idx, "pre_policy_p_nom_max_mw"] = old_p_nom_max
                network.generators.loc[gen_idx, "policy_land_cap_mw"] = land_cap_mw
                network.generators.loc[gen_idx, "policy_live_existing_capacity_mw"] = (
                    live_existing_capacity
                )
                network.generators.loc[gen_idx, "policy_effective_headroom_mw"] = (
                    effective_headroom
                )
                network.generators.loc[gen_idx, "policy_existing_oversubscribed"] = (
                    existing_oversubscribed
                )
                network.generators.loc[gen_idx, "policy_possible_preallocation_artifact"] = (
                    possible_preallocation_artifact
                )
                network.generators.loc[gen_idx, "p_nom_max"] = new_p_nom_max
                network.generators.loc[gen_idx, "post_policy_p_nom_max_mw"] = new_p_nom_max
                stats["constraints_applied"] += 1
                if not np.isclose(old_p_nom_max, new_p_nom_max):
                    stats["constraints_binding"] += 1
                    stats["candidate_constraints_tightened"] += 1

            stats["generators_affected"] += len(matching_candidates)
            stats["extendable_candidates_matched"] += len(matching_candidates)
            stats["fixed_baseline_matches"] += len(matching_fixed)

            logger.info(
                "✓ %s @ %s: land cap %.0f MW, fixed baseline %.0f MW, candidate headroom %.0f MW (%s candidates, %s fixed baseline)",
                carrier,
                zone_name,
                land_cap_mw,
                live_existing_capacity,
                effective_headroom,
                len(matching_candidates),
                len(matching_fixed),
            )
            continue

        if len(matching_fixed) > 0:
            for gen_idx in matching_fixed:
                old_p_nom_max = pd.to_numeric(
                    network.generators.loc[gen_idx, "p_nom_max"], errors="coerce"
                )
                old_p_nom_max = float(old_p_nom_max) if pd.notna(old_p_nom_max) else np.inf
                new_p_nom_max = min(old_p_nom_max, land_cap_mw)
                network.generators.loc[gen_idx, "p_nom_max"] = new_p_nom_max
                stats["constraints_applied"] += 1
                if not np.isclose(old_p_nom_max, new_p_nom_max):
                    stats["constraints_binding"] += 1

            stats["generators_affected"] += len(matching_fixed)
            stats["fixed_baseline_matches"] += len(matching_fixed)

            logger.info(
                "✓ %s @ %s: %.0f MW (legacy fallback on %s fixed generators)",
                carrier,
                zone_name,
                land_cap_mw,
                len(matching_fixed),
            )
            continue

        stats["no_matching_gens"] += 1
        stats["carrier_zone_mismatches"].append((carrier, zone_name))
        logger.warning(
            "⚠ %s @ %s: %.0f MW (NO MATCHING GENERATORS IN NETWORK)",
            carrier,
            zone_name,
            land_cap_mw,
        )

    logger.info("\n" + "-" * 80)
    logger.info("CONSTRAINT APPLICATION SUMMARY")
    logger.info("-" * 80)
    logger.info(f"Constraints loaded from CSV:        {stats['constraints_from_csv']}")
    logger.info(f"Mapped carrier-zone rows:           {stats['mapped_constraint_rows']}")
    logger.info(f"Generators found & constrained:     {stats['generators_affected']}")
    logger.info(f"Total constraints applied:          {stats['constraints_applied']}")
    logger.info(f"Constraints that are binding:       {stats['constraints_binding']}")
    logger.info(f"Exclusions (0 MW allowed):          {stats['zero_constraints']}")
    logger.info(f"Fixed baseline generators matched:  {stats['fixed_baseline_matches']}")
    logger.info(f"Extendable candidates matched:      {stats['extendable_candidates_matched']}")
    logger.info(f"Candidate constraints tightened:    {stats['candidate_constraints_tightened']}")
    logger.info(f"Oversubscribed candidate rows:      {stats['candidate_oversubscribed_rows']}")
    logger.info(
        "Possible pre-allocation artefacts:  %s",
        stats["possible_preallocation_artifact_rows"],
    )
    logger.info(f"Row-level candidate caps applied:   {stats['row_level_candidate_caps_applied']}")
    logger.info(f"Missing generator matches:          {stats['no_matching_gens']}")

    if stats["carrier_zone_mismatches"]:
        logger.warning("\nCarrier-zone entries without matching generators:")
        for carrier, zone_name in stats["carrier_zone_mismatches"]:
            logger.warning(f"  - {carrier} @ {zone_name}")

    logger.info("=" * 80 + "\n")
    return network, stats


def save_network(network, output_path):
    """Save PyPSA network to NetCDF."""
    logger.info(f"Saving constrained network to {output_path}")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    network.export_to_netcdf(output_path)
    logger.info("Network saved successfully")


def main():
    """Main Snakemake entry point."""
    apply_constraints = snakemake.params.apply_constraints
    network = load_network(snakemake.input.network)

    if not apply_constraints:
        logger.info("Technical potential constraints disabled")
        save_network(network, snakemake.output.network)

        summary_lines = [
            "=" * 80,
            "TECHNICAL POTENTIAL CONSTRAINTS APPLICATION REPORT",
            "=" * 80,
            f"\nNetwork: {snakemake.input.network}",
            f"Output Network: {snakemake.output.network}",
            "\nStatus: CONSTRAINTS DISABLED",
            "Reason: technical_potential_constraints.enabled = false in scenario config",
            "\nNetwork copied unchanged - no constraints applied",
            "\n" + "=" * 80,
        ]
        with open(snakemake.output.report, "w") as handle:
            handle.write("\n".join(summary_lines))
        logger.info(f"Report written to {snakemake.output.report}")
        return

    logger.info("Applying technical potential constraints")
    technical_potential = load_technical_potential(snakemake.input.technical_potential)
    network, stats = apply_technical_potential_constraints(network, technical_potential)
    save_network(network, snakemake.output.network)

    summary_lines = [
        "=" * 80,
        "TECHNICAL POTENTIAL CONSTRAINTS APPLICATION REPORT",
        "=" * 80,
        f"\nNetwork: {snakemake.input.network}",
        f"Technical Potential CSV: {snakemake.input.technical_potential}",
        f"Output Network: {snakemake.output.network}",
        "\nStatistics:",
        f"  Constraints from CSV:           {stats['constraints_from_csv']}",
        f"  Mapped carrier-zone rows:       {stats['mapped_constraint_rows']}",
        f"  Generators constrained:         {stats['generators_affected']}",
        f"  Constraints applied:            {stats['constraints_applied']}",
        f"  Binding constraints:            {stats['constraints_binding']}",
        f"  Technology exclusions:          {stats['zero_constraints']}",
        f"  Fixed baseline matched:         {stats['fixed_baseline_matches']}",
        f"  Extendable candidates matched:  {stats['extendable_candidates_matched']}",
        f"  Candidate constraints tightened:{stats['candidate_constraints_tightened']:>12}",
        f"  Oversubscribed candidate rows:  {stats['candidate_oversubscribed_rows']}",
        f"  Possible pre-allocation artefacts:{stats['possible_preallocation_artifact_rows']:>8}",
        f"  Row-level candidate caps:       {stats['row_level_candidate_caps_applied']}",
        f"  Unmatched constraint entries:   {stats['no_matching_gens']}",
        "\n" + "=" * 80,
    ]

    with open(snakemake.output.report, "w") as handle:
        handle.write("\n".join(summary_lines))

    logger.info(f"Report written to {snakemake.output.report}")


if __name__ == "__main__":
    main()
