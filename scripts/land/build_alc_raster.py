#!/usr/bin/env python3
"""
Build Agricultural Land Classification (ALC) BMV Raster for Great Britain.

Loads pre-filtered Best and Most Versatile (BMV) agricultural land
GeoPackages for England, Wales, and Scotland, merges them into a unified
binary raster at 100m resolution in EPSG:27700.

Input files are pre-filtered to contain only BMV features:
    - England: Grades 1, 2, 3a (Natural England post-1988 ALC, Defra)
    - Wales: Grades 1, 2, 3a (Welsh Government predictive ALC)
    - Scotland: LCA classes 1, 2, 3.1 (James Hutton Institute LCA 250K)

All three input layers are already in EPSG:27700. They are loaded,
merged into a single dissolved layer, and rasterized onto the canonical
GB reference grid shared by all foundation rasters. The output is a
binary mask where 1 indicates BMV agricultural land and 0 indicates
non-BMV.

BMV agricultural land is a **hard constraint** for solar and nuclear SMR
siting. UK planning policy (NPPF, PPW, SPP) strongly discourages siting
energy infrastructure on BMV agricultural land. Applied as an exclusion
zone — development on BMV land is excluded in the availability matrix.

Processing stages:
    1. Load England BMV boundaries (pre-filtered gpkg)
    2. Load Wales BMV boundaries (pre-filtered gpkg)
    3. Load Scotland BMV boundaries (pre-filtered gpkg)
    4. Merge into single GB GeoDataFrame, dissolve overlapping geometries
    5. Create canonical GB reference grid (shared across all foundation
       rasters)
    6. Rasterize dissolved BMV geometries as binary mask
    7. Write output: single-band GeoTIFF

Input:
    - data/land/societal/alc_bmv_england.gpkg — Pre-filtered BMV land
      for England (Grades 1, 2, 3a). Source: Natural England / Defra.
    - data/land/societal/alc_bmv_wales.gpkg — Pre-filtered BMV land
      for Wales (Grades 1, 2, 3a). Source: Welsh Government.
    - data/land/societal/alc_bmv_scotland.gpkg — Pre-filtered BMV land
      for Scotland (LCA classes 1, 2, 3.1). Source: James Hutton
      Institute.

Output:
    - resources/land/alc_bmv_gb.tif — single-band GeoTIFF (uint8,
      EPSG:27700, 100m resolution). Binary mask: 1 = BMV agricultural
      land, 0 = not BMV.

Author: K O'Neill
Date: 2026-03-10
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
    create_reference_grid,
    dissolve_overlaps,
    load_and_reproject_vector,
    rasterize_vector,
    validate_gb_coverage,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "build_alc_raster"
)
logger = setup_logging(log_path)


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
        raise FileNotFoundError(
            f"Missing {len(missing)} input file(s):\n"
            + "\n".join(missing)
        )

    logger.info(
        f"All {len(input_paths)} input files validated."
    )


def load_all_bmv_data(
    input_paths: dict[str, str],
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load and merge pre-filtered BMV GeoPackages into a single
    GeoDataFrame.

    Each input file contains only BMV features (pre-filtered in
    the data preparation notebook). This function loads each,
    tags with a data_source for provenance, and concatenates.

    Parameters
    ----------
    input_paths : dict of str to str
        Mapping of input names to file paths. Expected keys:
        ``alc_eng``, ``alc_wal``, ``alc_sco``.
    target_crs : str, optional
        Target coordinate reference system, by default
        "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Merged BMV geometries with columns: geometry,
        data_source. CRS is ``target_crs``. Not yet dissolved
        — overlapping geometries from different sources may
        exist at nation borders.
    """
    source_labels = {
        "alc_eng": "NaturalEngland",
        "alc_wal": "WelshGov",
        "alc_sco": "ScotGov",
    }

    datasets = []
    for key, label in source_labels.items():
        path = input_paths[key]
        logger.info(
            f"Loading {label} BMV from {Path(path).name}"
        )
        gdf = load_and_reproject_vector(
            path, target_crs=target_crs
        )
        logger.info(f"  {label}: {len(gdf)} BMV features")
        gdf["data_source"] = label
        datasets.append(gdf[["geometry", "data_source"]])

    all_bmv = gpd.GeoDataFrame(
        pd.concat(datasets, ignore_index=True),
        crs=target_crs,
    )
    logger.info(
        f"All BMV data merged: {len(all_bmv)} features "
        f"from {len(datasets)} datasets"
    )

    source_counts = all_bmv["data_source"].value_counts()
    for source, count in source_counts.items():
        logger.info(f"  {source}: {count} features")

    return all_bmv


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_alc_raster(
    all_bmv: gpd.GeoDataFrame,
    resolution: int,
    target_crs: str = "EPSG:27700",
) -> tuple[np.ndarray, dict]:
    """
    Dissolve merged BMV geometries and rasterize into a binary mask.

    Takes the merged (but not yet dissolved) BMV GeoDataFrame from
    :func:`load_all_bmv_data`, dissolves overlapping geometries,
    creates the canonical GB reference grid, and rasterizes to a
    binary mask.

    Parameters
    ----------
    all_bmv : gpd.GeoDataFrame
        Merged BMV geometries from England, Wales, and Scotland.
        Must have a ``geometry`` column and CRS matching ``target_crs``.
    resolution : int
        Raster resolution in metres.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(bmv_raster, profile)`` where *bmv_raster* has shape
        ``(height, width)`` with uint8 values (1 = BMV, 0 = not BMV),
        and *profile* is a rasterio profile dict.
    """
    # Dissolve overlapping geometries
    logger.info("Dissolving overlapping BMV geometries...")
    pre_dissolve_count = len(all_bmv)
    all_bmv_dissolved = dissolve_overlaps(all_bmv)
    logger.info(f"Dissolved: {pre_dissolve_count} features → {len(all_bmv_dissolved)} features")

    # Validate GB coverage
    validate_gb_coverage(all_bmv_dissolved)

    # Create canonical GB reference grid
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )
    template = (width, height, transform, crs)

    # Rasterize as binary mask
    logger.info("Rasterizing BMV geometries as binary mask...")
    bmv_raster = rasterize_vector(
        all_bmv_dissolved,
        template,
        burn_value=1,
        dtype="uint8",
    )

    # Build rasterio profile
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": target_crs,
        "transform": transform,
    }

    # Log summary statistics
    bmv_pixels = np.count_nonzero(bmv_raster)
    total_pixels = bmv_raster.size
    bmv_pct = 100.0 * bmv_pixels / total_pixels if total_pixels > 0 else 0.0
    logger.info(
        f"ALC BMV raster: {bmv_raster.shape}, "
        f"BMV pixels: {bmv_pixels} of {total_pixels} ({bmv_pct:.2f}%)"
    )

    return bmv_raster, profile


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD ALC BMV RASTER")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "alc_eng": snk.input.alc_eng,
            "alc_wal": snk.input.alc_wal,
            "alc_sco": snk.input.alc_sco,
        }
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        input_paths = {
            "alc_eng": "data/land/societal/alc_bmv_england.gpkg",
            "alc_wal": "data/land/societal/alc_bmv_wales.gpkg",
            "alc_sco": "data/land/societal/alc_bmv_scotland.gpkg",
        }
        output_raster = "resources/land/alc_bmv_gb.tif"
        resolution = 100
        target_crs = 27700

    crs_str = f"EPSG:{target_crs}"
    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 0: Validate inputs
    stage_start = time.time()
    validate_inputs(input_paths)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 1: Load and classify all ALC data
    stage_start = time.time()
    all_bmv = load_all_bmv_data(
        input_paths=input_paths,
        target_crs=crs_str,
    )
    stage_times["Load and classify ALC data"] = time.time() - stage_start

    # Stage 2: Dissolve, grid, rasterize
    stage_start = time.time()
    bmv_raster, profile = build_alc_raster(
        all_bmv=all_bmv,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 3: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(bmv_raster, profile, output_raster)
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build ALC BMV Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "raster_shape": str(bmv_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD ALC BMV RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
