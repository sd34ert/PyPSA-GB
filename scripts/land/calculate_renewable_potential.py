#!/usr/bin/env python3
"""
Calculate renewable technical potential (MW) per zone per technology.

Converts availability fractions (onshore) and available areas (offshore)
into maximum installable capacity (p_nom_max) using technology-specific
capacity densities from config.

Onshore (onwind, solar):
    p_nom_max = availability_fraction × zone_area_km2 × capacity_density

Offshore (offwind-fixed-ac/dc, offwind-float-ac/dc):
    Aggregates KRA fragments by (zone, carrier, cost_tier):
    p_nom_max = sum(available_area_km2) × capacity_density
    Plus area-weighted centroid and distance for bus placement.

Input:
    - resources/land/availability_matrix_{network_model}.csv — onshore
      fractional availability per zone per technology
    - resources/land/offshore_available_kra_{network_model}.gpkg — offshore
      KRA fragments with TG, cost tier, carrier, available area
    - resources/land/zone_statistics_{network_model}.csv — zone areas (km²)
    - Config: capacity densities from defaults.yaml

Output:
    - resources/land/renewable_technical_potential_{network_model}.csv —
      combined onshore + offshore potential with all attributes

Author: K O'Neill
Date: 2026-03-23
"""

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

# Project path setup
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

try:
    from scripts.utilities.logging_config import (
        log_execution_summary,
        log_stage_summary,
        setup_logging,
    )
except ImportError:
    logging.basicConfig(level=logging.INFO)

    def setup_logging(name):
        return logging.getLogger(name)

    def log_stage_summary(*args, **kwargs):
        pass

    def log_execution_summary(*args, **kwargs):
        pass


# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "calculate_renewable_potential"
)
logger = setup_logging(log_path)

# Output column order
OUTPUT_COLUMNS = [
    "zone_name",
    "carrier",
    "cost_tier",
    "p_nom_max_mw",
    "capacity_density",
    "capex_multiplier",
    "connection_type",
    "distance_to_coast_km",
    "centroid_x",
    "centroid_y",
]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_capacity_density(carrier, config):
    """Look up capacity density (MW/km²) for a carrier from config.

    Parameters
    ----------
    carrier : str
        Technology carrier name (e.g. 'onwind', 'offwind-fixed-ac').
    config : dict
        Land constraints config dict from defaults.yaml.

    Returns
    -------
    float
        Capacity density in MW/km².

    Raises
    ------
    ValueError
        If carrier not found in config.
    """
    # Direct lookup for onshore
    if carrier in config:
        tech_config = config[carrier]
        if isinstance(tech_config, dict):
            density = tech_config.get("capacity_density")
            if isinstance(density, dict):
                # Floating has nested ac/dc densities — shouldn't reach here
                # since we look up by full carrier name
                raise ValueError(
                    f"Nested capacity_density for '{carrier}'. "
                    f"Use full carrier name (e.g. offwind-float-ac)."
                )
            if density is not None:
                return float(density)

    # Offshore carriers: map to config section
    carrier_config_map = {
        "offwind-fixed-ac": ("offwind-ac", "capacity_density"),
        "offwind-fixed-dc": ("offwind-dc", "capacity_density"),
        "offwind-float-ac": ("offwind-float", "capacity_density", "ac"),
        "offwind-float-dc": ("offwind-float", "capacity_density", "dc"),
    }

    if carrier in carrier_config_map:
        keys = carrier_config_map[carrier]
        section = config.get(keys[0], {})
        density = section.get(keys[1])
        if len(keys) == 3 and isinstance(density, dict):
            density = density.get(keys[2])
        if density is not None:
            return float(density)

    raise ValueError(
        f"No capacity_density found for carrier '{carrier}' in config. "
        f"Available sections: {list(config.keys())}"
    )


def calculate_onshore_potential(avail_df, zone_areas, config):
    """Calculate onshore technical potential per zone per technology.

    Parameters
    ----------
    avail_df : pd.DataFrame
        Availability matrix with zone_name index and technology columns
        (onwind, solar) containing fractions 0.0–1.0.
    zone_areas : pd.Series
        Zone areas in km², indexed by zone name.
    config : dict
        Land constraints config from defaults.yaml.

    Returns
    -------
    pd.DataFrame
        One row per (zone, carrier) with p_nom_max_mw and capacity_density.
    """
    rows = []
    technologies = [col for col in avail_df.columns if col in ("onwind", "solar")]

    for tech in technologies:
        density = get_capacity_density(tech, config)
        logger.info(f"  {tech}: capacity_density = {density} MW/km²")

        for zone_name, frac in avail_df[tech].items():
            area = zone_areas.get(zone_name, 0.0)
            p_nom_max = frac * area * density

            rows.append({
                "zone_name": zone_name,
                "carrier": tech,
                "cost_tier": "",
                "p_nom_max_mw": round(p_nom_max, 2),
                "capacity_density": density,
                "capex_multiplier": np.nan,
                "connection_type": "",
                "distance_to_coast_km": np.nan,
                "centroid_x": np.nan,
                "centroid_y": np.nan,
            })

    result = pd.DataFrame(rows)
    logger.info(
        f"  Onshore: {len(result)} rows, "
        f"{result['p_nom_max_mw'].sum():.0f} MW total"
    )
    return result


def calculate_offshore_potential(kra_gdf, config):
    """Calculate offshore technical potential by aggregating KRA fragments.

    Groups by (zone_name, carrier, cost_tier) and computes:
    - p_nom_max_mw = sum(available_area_km2) × capacity_density
    - Area-weighted mean distance_to_coast_km
    - Area-weighted centroid (centroid_x, centroid_y)

    Parameters
    ----------
    kra_gdf : gpd.GeoDataFrame
        Offshore KRA fragments from build_offshore_available_kra.
    config : dict
        Land constraints config from defaults.yaml.

    Returns
    -------
    pd.DataFrame
        One row per (zone, carrier, cost_tier) with all attributes.
    """
    # Drop geometry for tabular aggregation
    df = pd.DataFrame(kra_gdf.drop(columns="geometry"))

    group_cols = ["zone_name", "carrier", "cost_tier"]
    rows = []

    for (zone, carrier, tier), group in df.groupby(group_cols):
        total_area = group["available_area_km2"].sum()
        weights = group["available_area_km2"]

        # Area-weighted means
        if total_area > 0:
            w_dist = np.average(
                group["distance_to_coast_km"], weights=weights
            )
            w_cx = np.average(group["centroid_x"], weights=weights)
            w_cy = np.average(group["centroid_y"], weights=weights)
        else:
            w_dist = group["distance_to_coast_km"].mean()
            w_cx = group["centroid_x"].mean()
            w_cy = group["centroid_y"].mean()

        density = get_capacity_density(carrier, config)
        p_nom_max = total_area * density

        rows.append({
            "zone_name": zone,
            "carrier": carrier,
            "cost_tier": tier,
            "p_nom_max_mw": round(p_nom_max, 2),
            "capacity_density": density,
            "capex_multiplier": group["capex_multiplier"].iloc[0],
            "connection_type": group["connection_type"].iloc[0],
            "distance_to_coast_km": round(w_dist, 2),
            "centroid_x": round(w_cx, 1),
            "centroid_y": round(w_cy, 1),
        })

    if not rows:
        result = pd.DataFrame(columns=OUTPUT_COLUMNS)
        logger.info("  Offshore: 0 rows (no KRA fragments)")
        return result

    result = pd.DataFrame(rows)
    logger.info(
        f"  Offshore: {len(result)} rows, "
        f"{result['p_nom_max_mw'].sum():.0f} MW total"
    )
    return result


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("CALCULATE RENEWABLE TECHNICAL POTENTIAL")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        onshore_path = snk.input.onshore_matrix
        offshore_path = snk.input.offshore_kra
        zone_stats_path = snk.input.zone_stats
        output_path = snk.output.potential
        config = snk.params.config
    else:
        onshore_path = "resources/land/availability_matrix_Zonal.csv"
        offshore_path = "resources/land/offshore_available_kra_Zonal.gpkg"
        zone_stats_path = "resources/land/zone_statistics_Zonal.csv"
        output_path = "resources/land/renewable_technical_potential_Zonal.csv"
        # Load defaults.yaml for standalone mode
        import yaml
        defaults_path = project_root / "config" / "defaults.yaml"
        if defaults_path.exists():
            with open(defaults_path) as f:
                config = yaml.safe_load(f).get("land_constraints", {})
        else:
            config = {}

    # Stage 1: Validate inputs
    stage_start = time.time()
    for name, path in [
        ("onshore_matrix", onshore_path),
        ("offshore_kra", offshore_path),
        ("zone_stats", zone_stats_path),
    ]:
        if not Path(path).exists():
            raise FileNotFoundError(f"Input not found: {name} → {path}")
        logger.info(f"  Input {name}: {path}")
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Load data
    stage_start = time.time()
    avail_df = pd.read_csv(onshore_path, index_col="zone_name")
    logger.info(
        f"  Onshore availability: {avail_df.shape[0]} zones, "
        f"technologies: {list(avail_df.columns)}"
    )

    kra_gdf = gpd.read_file(offshore_path)
    logger.info(f"  Offshore KRA fragments: {len(kra_gdf)} rows")

    zone_stats = pd.read_csv(zone_stats_path)
    zone_areas = zone_stats.set_index("zone")["area_km2"]
    logger.info(f"  Zone areas: {len(zone_areas)} zones")
    stage_times["Load data"] = time.time() - stage_start

    # Stage 3: Calculate onshore potential
    stage_start = time.time()
    logger.info("Calculating onshore potential...")
    onshore_df = calculate_onshore_potential(avail_df, zone_areas, config)
    stage_times["Onshore potential"] = time.time() - stage_start

    # Stage 4: Calculate offshore potential
    stage_start = time.time()
    logger.info("Calculating offshore potential...")
    offshore_df = calculate_offshore_potential(kra_gdf, config)
    stage_times["Offshore potential"] = time.time() - stage_start

    # Stage 5: Combine and write output
    stage_start = time.time()
    combined = pd.concat([onshore_df, offshore_df], ignore_index=True)
    combined = combined[OUTPUT_COLUMNS]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(output_path, index=False)
    logger.info(f"Output written to {output_path}")
    logger.info(f"  Total rows: {len(combined)}")
    logger.info(f"  Total potential: {combined['p_nom_max_mw'].sum():.0f} MW")

    # Log breakdown by carrier
    for carrier, group in combined.groupby("carrier"):
        logger.info(
            f"  {carrier}: {len(group)} rows, "
            f"{group['p_nom_max_mw'].sum():.0f} MW"
        )

    stage_times["Write output"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Calculate Renewable Potential",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "onshore_rows": len(onshore_df),
            "offshore_rows": len(offshore_df),
            "total_mw": f"{combined['p_nom_max_mw'].sum():.0f}",
        },
    )

    logger.info("=" * 80)
    logger.info("CALCULATE RENEWABLE TECHNICAL POTENTIAL — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
