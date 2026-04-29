#!/usr/bin/env python3
"""
Build Protected Areas Raster for Great Britain.

Merges protected area and environmental designation vector datasets
(from England, Scotland, and Wales) into a standardised 4-band GeoTIFF
raster at 100m resolution in EPSG:27700. Each band represents a
designation tier. Downstream technology configs enable/disable each
tier as a hard exclusion.

All Tier 1 inputs use land-only versions (excluding marine designations)
to prevent coastal zone inflation.

Band definitions:
    Band 1 — SAC + SPA + Ramsar + SSSI (excluding marine areas)
    Band 2 — Landscape designations: AONB + National Scenic Areas +
             National Parks
    Band 3 — Irreplaceable habitats: Ancient Woodland + blanket bog +
             limestone pavement + coastal sand dunes + lowland fens
    Band 4 — Historic environment: World Heritage Sites + Scheduled
             Monuments + Registered Parks & Gardens + Battlefields

Processing stages:
    1. Load and merge vector files by tier, reproject to EPSG:27700
    2. Dissolve overlapping geometries within each tier
    3. Create canonical GB reference grid (shared across all foundation rasters)
    4. Rasterize each tier as a separate band (binary: 1 = protected, 0 = not)
    5. Calculate zone-level fractions (% of each zone covered by each tier)
    6. Write outputs: 4-band GeoTIFF + zone fractions CSV

Input:
    Tier 1 (6 files): SAC, SPA, Ramsar, SSSI Eng/Sco/Wal (all excluding marine)
    Tier 2 (6 files): AONB Eng/Wal, NSA Sco, NatParks Eng/Wal/Sco
    Tier 3 (9 files): Ancient Woodland Eng/Sco/Wal, irreplaceable habitats
                      Eng/Sco, Wales blanket bog/dunes/limestone/fens
    Tier 4 (3 files): Historic environment Eng/Sco/Wal

Output:
    - resources/land/protected_areas_gb.tif — 4-band GeoTIFF (uint8, EPSG:27700)
    - resources/land/protected_area_fractions.csv — zone-level fractions per tier

Author: K O'Neill
Date: 2026-02-27
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


from scripts.utilities.land_utils import (
    calculate_zone_fraction,
    create_reference_grid,
    dissolve_overlaps,
    load_zone_shapes,
    merge_national_datasets,
    rasterize_vector,
    validate_gb_coverage,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_protected_areas_raster"
logger = setup_logging(log_path)

# Band names for the output GeoTIFF
BAND_NAMES = [
    "SAC_SPA_Ramsar_SSSI",
    "AONB_NatParks_NSA",
    "Irreplaceable_Habitats",
    "Historic_Environment",
]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def validate_inputs(input_paths: dict[str, str]) -> None:
    """
    Validate that all input files exist and are readable.

    Parameters
    ----------
    input_paths : dict of str to str
        Mapping of input name to file path.

    Raises
    ------
    FileNotFoundError
        If any input file does not exist.
    """
    missing = []
    for name, path in input_paths.items():
        if not Path(path).exists():
            missing.append(f"  {name}: {path}")

    if missing:
        raise FileNotFoundError(f"Missing {len(missing)} input file(s):\n" + "\n".join(missing))

    logger.info(f"All {len(input_paths)} input files validated.")


def load_and_merge_tier(
    paths: list[str],
    tier_name: str,
) -> gpd.GeoDataFrame:
    """
    Load, merge, and dissolve vector files for a single protection tier.

    Parameters
    ----------
    paths : list of str
        Paths to vector files for this tier.
    tier_name : str
        Human-readable tier name for logging.

    Returns
    -------
    gpd.GeoDataFrame
        Merged and dissolved GeoDataFrame in EPSG:27700.
    """
    logger.info(f"Loading {tier_name}: {len(paths)} file(s)")
    gdf = merge_national_datasets(paths, target_crs="EPSG:27700")

    # Log coverage but don't enforce — individual tiers (e.g. Ramsar)
    # legitimately cover only a fraction of GB
    validate_gb_coverage(gdf, min_fraction=0.0)

    gdf = dissolve_overlaps(gdf)
    logger.info(f"{tier_name}: {len(gdf)} non-overlapping polygons after dissolve")

    return gdf


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_protected_areas_raster(
    tier1_paths: list[str],
    tier2_paths: list[str],
    tier3_paths: list[str],
    tier4_paths: list[str],
    resolution: int,
    target_crs: int,
) -> tuple[np.ndarray, dict]:
    """
    Merge protected area datasets and rasterize into a 4-band GeoTIFF.

    Parameters
    ----------
    tier1_paths : list of str
        Paths to Natura 2000 vector files (SAC, SPA, Ramsar).
    tier2_paths : list of str
        Paths to SSSI vector files (England, Scotland, Wales).
    tier3_paths : list of str
        Paths to landscape designation files (AONB, NSA, National Parks).
    tier4_paths : list of str
        Paths to ancient woodland files (England, Scotland, Wales).
    resolution : int
        Raster resolution in metres.
    target_crs : int
        EPSG code for the target CRS.

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(raster_array, profile)`` where *raster_array* has shape
        ``(4, height, width)`` and *profile* is a rasterio profile dict.
    """
    crs_str = f"EPSG:{target_crs}"

    # Stage 1: Load and merge by tier
    tier1_gdf = load_and_merge_tier(
        tier1_paths,
        "Tier 1 (SAC+SPA+Ramsar+SSSI, excl. marine)",
    )
    tier2_gdf = load_and_merge_tier(
        tier2_paths, "Tier 2 (AONB+NSA+National Parks)",
    )
    tier3_gdf = load_and_merge_tier(
        tier3_paths, "Tier 3 (Irreplaceable Habitats)",
    )
    tier4_gdf = load_and_merge_tier(
        tier4_paths, "Tier 4 (Historic Environment)",
    )

    # Stage 2: Create reference grid (canonical GB bounds)
    width, height, transform, crs = create_reference_grid(resolution=resolution, crs=crs_str)

    template = (width, height, transform, crs)

    # Stage 3: Rasterize each tier as separate band
    logger.info("Rasterizing Tier 1 (SAC/SPA/Ramsar/SSSI)...")
    band1 = rasterize_vector(tier1_gdf, template, burn_value=1, dtype="uint8")

    logger.info("Rasterizing Tier 2 (AONB/NatParks/NSA)...")
    band2 = rasterize_vector(tier2_gdf, template, burn_value=1, dtype="uint8")

    logger.info("Rasterizing Tier 3 (Irreplaceable Habitats)...")
    band3 = rasterize_vector(tier3_gdf, template, burn_value=1, dtype="uint8")

    logger.info("Rasterizing Tier 4 (Historic Environment)...")
    band4 = rasterize_vector(tier4_gdf, template, burn_value=1, dtype="uint8")

    # Stack into 4-band array: shape (4, height, width)
    protected_raster = np.stack([band1, band2, band3, band4], axis=0)

    # Build rasterio profile
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 4,
        "crs": crs_str,
        "transform": transform,
    }

    logger.info(
        f"Protected areas raster: {protected_raster.shape}, "
        f"total protected pixels per tier: "
        f"T1={np.count_nonzero(band1)}, T2={np.count_nonzero(band2)}, "
        f"T3={np.count_nonzero(band3)}, T4={np.count_nonzero(band4)}"
    )

    return protected_raster, profile


def calculate_zone_fractions(
    raster: np.ndarray,
    transform,
    zone_path: str | None = None,
    zones: gpd.GeoDataFrame | None = None,
) -> pd.DataFrame:
    """
    Calculate the fraction of each zone covered by each protection tier.

    Parameters
    ----------
    raster : np.ndarray
        4-band raster array of shape ``(4, height, width)``.
    transform : Affine
        Affine transform for the raster.
    zone_path : str, optional
        Path to zone shapes file. Either this or *zones* must be provided.
    zones : gpd.GeoDataFrame, optional
        Pre-loaded zone shapes. Either this or *zone_path* must be provided.

    Returns
    -------
    pd.DataFrame
        DataFrame with columns: zone, tier1_frac, tier2_frac, tier3_frac,
        tier4_frac. All fractions are in the range [0.0, 1.0].
    """
    if zones is None:
        if zone_path is None:
            raise ValueError("Either zone_path or zones must be provided.")
        zones = load_zone_shapes(zone_path, target_crs="EPSG:27700")

    tier_names = ["tier1_frac", "tier2_frac", "tier3_frac", "tier4_frac"]
    zone_fractions = pd.DataFrame({"zone": zones["zone_name"].values})

    for i, tier_name in enumerate(tier_names):
        logger.info(f"Calculating zone fractions for {BAND_NAMES[i]}...")
        zone_fractions[tier_name] = calculate_zone_fraction(raster[i], zones, transform).values

    logger.info(f"Zone fractions calculated for {len(zones)} zones across {len(tier_names)} tiers.")

    return zone_fractions


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD PROTECTED AREAS RASTER")
    logger.info("=" * 80)

    # Get parameters
    env = "data/land/environment"
    if snk:
        input_paths = {
            # Tier 1: SAC/SPA/Ramsar/SSSI (excluding marine)
            "sac": snk.input.sac,
            "spa": snk.input.spa,
            "ramsar": snk.input.ramsar,
            "sssi_eng": snk.input.sssi_eng,
            "sssi_sco": snk.input.sssi_sco,
            "sssi_wal": snk.input.sssi_wal,
            # Tier 2: AONB/NatParks/NSA
            "aonb_eng": snk.input.aonb_eng,
            "aonb_wal": snk.input.aonb_wal,
            "nsa_sco": snk.input.nsa_sco,
            "natpark_eng": snk.input.natpark_eng,
            "natpark_wal": snk.input.natpark_wal,
            "natpark_sco": snk.input.natpark_sco,
            # Tier 3: Irreplaceable habitats
            "aw_eng": snk.input.aw_eng,
            "aw_sco": snk.input.aw_sco,
            "aw_wal": snk.input.aw_wal,
            "irr_eng": snk.input.irr_eng,
            "irr_sco": snk.input.irr_sco,
            "irr_bog_wal": snk.input.irr_bog_wal,
            "irr_dunes_wal": snk.input.irr_dunes_wal,
            "irr_lime_wal": snk.input.irr_lime_wal,
            "irr_fens_wal": snk.input.irr_fens_wal,
            # Tier 4: Historic environment
            "hist_eng": snk.input.hist_eng,
            "hist_sco": snk.input.hist_sco,
            "hist_wal": snk.input.hist_wal,
        }
        output_raster = snk.output.raster
        output_fractions = snk.output.zone_fractions
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        input_paths = {
            # Tier 1: SAC/SPA/Ramsar/SSSI (excluding marine)
            "sac": f"{env}/gb_sac_excluding_marine.gpkg",
            "spa": f"{env}/gb_spa_excluding_marine.gpkg",
            "ramsar": f"{env}/gb_ramsar_excluding_marine.gpkg",
            "sssi_eng": f"{env}/sssi_england_excluding_marine.gpkg",
            "sssi_sco": f"{env}/sssi_scotland_excluding_marine.gpkg",
            "sssi_wal": f"{env}/sssi_wales_excluding_marine.gpkg",
            # Tier 2: AONB/NatParks/NSA
            "aonb_eng": f"{env}/aonb_england.gpkg",
            "aonb_wal": f"{env}/aonb_wales.gpkg",
            "nsa_sco": f"{env}/nsa_scotland.shp",
            "natpark_eng": f"{env}/national_parks_england.gpkg",
            "natpark_wal": f"{env}/national_parks_wales.gpkg",
            "natpark_sco": f"{env}/national_parks_scotland.gpkg",
            # Tier 3: Irreplaceable habitats
            "aw_eng": f"{env}/ancient_woodland_england.gpkg",
            "aw_sco": f"{env}/ancient_woodland_scotland.gpkg",
            "aw_wal": f"{env}/ancient_woodland_wales.gpkg",
            "irr_eng": f"{env}/irreplaceable_habitats_england.gpkg",
            "irr_sco": f"{env}/irreplaceable_habitats_scotland.gpkg",
            "irr_bog_wal": f"{env}/irreplaceable_habitats_blanket_bog_wales.gpkg",
            "irr_dunes_wal": f"{env}/coastal_sand_dunes_wales.gpkg",
            "irr_lime_wal": f"{env}/irreplaceable_habitats_limestone_pavement_wales.gpkg",
            "irr_fens_wal": f"{env}/irreplaceable_habitats_lowland_fens_wales.gpkg",
            # Tier 4: Historic environment
            "hist_eng": f"{env}/historic_environment_england.gpkg",
            "hist_sco": f"{env}/historic_environment_scotland.gpkg",
            "hist_wal": f"{env}/historic_environment_wales.gpkg",
        }
        output_raster = "resources/land/protected_areas_gb.tif"
        output_fractions = "resources/land/protected_area_fractions.csv"
        resolution = 100
        target_crs = 27700

    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: EPSG:{target_crs}")

    # Stage 1: Validate inputs
    stage_start = time.time()
    validate_inputs(input_paths)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Organise paths by tier
    tier1_paths = [
        input_paths["sac"], input_paths["spa"],
        input_paths["ramsar"],
        input_paths["sssi_eng"], input_paths["sssi_sco"],
        input_paths["sssi_wal"],
    ]
    tier2_paths = [
        input_paths["aonb_eng"], input_paths["aonb_wal"],
        input_paths["nsa_sco"],
        input_paths["natpark_eng"],
        input_paths["natpark_wal"],
        input_paths["natpark_sco"],
    ]
    tier3_paths = [
        input_paths["aw_eng"], input_paths["aw_sco"],
        input_paths["aw_wal"],
        input_paths["irr_eng"], input_paths["irr_sco"],
        input_paths["irr_bog_wal"],
        input_paths["irr_dunes_wal"],
        input_paths["irr_lime_wal"],
        input_paths["irr_fens_wal"],
    ]
    tier4_paths = [
        input_paths["hist_eng"],
        input_paths["hist_sco"],
        input_paths["hist_wal"],
    ]

    # Stage 2: Build raster
    stage_start = time.time()
    protected_raster, profile = build_protected_areas_raster(
        tier1_paths=tier1_paths,
        tier2_paths=tier2_paths,
        tier3_paths=tier3_paths,
        tier4_paths=tier4_paths,
        resolution=resolution,
        target_crs=target_crs,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 3: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(
        protected_raster,
        profile,
        output_raster,
        band_names=BAND_NAMES,
    )
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Stage 4: Calculate zone fractions (only if zone shapes available)
    stage_start = time.time()
    if snk and hasattr(snk.input, "zones"):
        zone_fractions = calculate_zone_fractions(
            raster=protected_raster,
            transform=profile["transform"],
            zone_path=snk.input.zones,
        )
        zone_fractions.to_csv(output_fractions, index=False)
        logger.info(f"Zone fractions saved to {output_fractions}")
    else:
        logger.info("No zone shapes input provided — skipping zone fractions calculation.")
    stage_times["Zone fractions"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Protected Areas Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "raster_shape": str(protected_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD PROTECTED AREAS RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
