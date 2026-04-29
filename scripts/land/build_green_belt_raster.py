#!/usr/bin/env python3
"""
Build Green Belt Raster for Great Britain.

Merges Green Belt boundary data from England and Scotland into a unified
binary raster at 100m resolution in EPSG:27700. England's Green Belt data
is sourced from DLUHC (Department for Levelling Up, Housing and Communities)
as a GeoPackage. Scotland's Green Belt data is sourced from the Scottish
Government as a GeoPackage. Wales has a single greenbelt between Cardiff &
Newport. This area is small, data is not easily accessible & therefore
ignored.

Both input layers are reprojected to EPSG:27700, merged into a single
dissolved layer, and rasterized onto the canonical GB reference grid shared
by all foundation rasters. The output is a binary mask where 1 indicates
Green Belt land and 0 indicates non-Green Belt.

Green Belt is a **soft constraint** for solar and nuclear SMR siting.
"Very special circumstances" required for development on green belt areas
unless community projects. It is assumed that large scale projects are not
sited on green belt areas. Siting of energy projects in green belt areas
is applied as increased siting difficulty rather than absolute prohibition.

Processing stages:
    1. Load England Green Belt boundaries (DLUHC, gpkg)
    2. Load Scotland Green Belt boundaries (Scottish Government, gpkg)
    3. Merge into single GB GeoDataFrame, dissolve overlapping geometries
    4. Create canonical GB reference grid (shared across all foundation
       rasters)
    5. Rasterize dissolved Green Belt geometries as binary mask
    6. Write output: single-band GeoTIFF

Input:
    - data/land/societal/green_belt_england.gpkg — DLUHC Green Belt
      boundaries for England. Source: planning.data.gov.uk.
    - data/land/societal/green_belt_scotland.gpkg — Scottish Government
      Green Belt boundaries for Scotland. Source: spatialdata.gov.scot.

Output:
    - resources/land/green_belt_gb.tif — single-band GeoTIFF (uint8,
      EPSG:27700, 100m resolution). Binary mask: 1 = Green Belt,
      0 = not Green Belt.

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
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_green_belt_raster"
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
        raise FileNotFoundError(f"Missing {len(missing)} input file(s):\n" + "\n".join(missing))

    logger.info(f"All {len(input_paths)} input files validated.")


def load_all_green_belt_data(
    input_paths: dict[str, str],
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load and merge all GB Green Belt datasets into a single GeoDataFrame.

    Loads Green Belt boundary data from England (DLUHC, gpkg) and Scotland
    (Scottish Government, shp), reprojects each to the target CRS, and
    concatenates into a single GeoDataFrame. Each feature is tagged with
    its data source for provenance. Wales has no statutory Green Belt.

    Parameters
    ----------
    input_paths : dict of str to str
        Mapping of input names to file paths. Expected keys:
        ``gb_eng``, ``gb_sco``.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Merged Green Belt geometries with columns: geometry, data_source.
        CRS is ``target_crs``. Not yet dissolved — overlapping geometries
        from different sources may exist.
    """
    datasets = []

    # England — DLUHC Green Belt boundaries
    logger.info(f"Loading England Green Belt from {Path(input_paths['gb_eng']).name}")
    gb_eng = load_and_reproject_vector(input_paths["gb_eng"], target_crs=target_crs)
    logger.info(f"  England Green Belt: {len(gb_eng)} features")
    gb_eng["data_source"] = "DLUHC"
    datasets.append(gb_eng[["geometry", "data_source"]])

    # Scotland — Scottish Government Green Belt boundaries
    logger.info(f"Loading Scotland Green Belt from {Path(input_paths['gb_sco']).name}")
    gb_sco = load_and_reproject_vector(input_paths["gb_sco"], target_crs=target_crs)
    logger.info(f"  Scotland Green Belt: {len(gb_sco)} features")
    gb_sco["data_source"] = "ScotGov"
    datasets.append(gb_sco[["geometry", "data_source"]])

    # Merge all into single GeoDataFrame
    all_gb = gpd.GeoDataFrame(
        pd.concat(datasets, ignore_index=True),
        crs=target_crs,
    )
    total_features = len(all_gb)
    logger.info(f"All Green Belt data merged: {total_features} features from 2 datasets")

    # Log per-source counts
    source_counts = all_gb["data_source"].value_counts()
    for source, count in source_counts.items():
        logger.info(f"  {source}: {count} features")

    return all_gb


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_green_belt_raster(
    all_gb: gpd.GeoDataFrame,
    resolution: int,
    target_crs: str = "EPSG:27700",
) -> tuple[np.ndarray, dict]:
    """
    Dissolve merged Green Belt geometries and rasterize into a binary mask.

    Takes the merged (but not yet dissolved) Green Belt GeoDataFrame from
    :func:`load_all_green_belt_data`, dissolves overlapping geometries,
    creates the canonical GB reference grid, and rasterizes to a binary mask.

    Parameters
    ----------
    all_gb : gpd.GeoDataFrame
        Merged Green Belt geometries from England and Scotland.
        Must have a ``geometry`` column and CRS matching ``target_crs``.
    resolution : int
        Raster resolution in metres.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(gb_raster, profile)`` where *gb_raster* has shape
        ``(height, width)`` with uint8 values (1 = Green Belt,
        0 = not Green Belt), and *profile* is a rasterio profile dict.
    """
    # Stage 1b: Dissolve overlapping geometries
    logger.info("Dissolving overlapping Green Belt geometries...")
    pre_dissolve_count = len(all_gb)
    all_gb_dissolved = dissolve_overlaps(all_gb)
    logger.info(f"Dissolved: {pre_dissolve_count} features → {len(all_gb_dissolved)} features")

    # Validate GB coverage
    validate_gb_coverage(all_gb_dissolved)

    # Stage 2: Create canonical GB reference grid
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )
    template = (width, height, transform, crs)

    # Stage 3: Rasterize as binary mask
    logger.info("Rasterizing Green Belt geometries as binary mask...")
    gb_raster = rasterize_vector(
        all_gb_dissolved,
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
    gb_pixels = np.count_nonzero(gb_raster)
    total_pixels = gb_raster.size
    gb_pct = 100.0 * gb_pixels / total_pixels if total_pixels > 0 else 0.0
    logger.info(
        f"Green Belt raster: {gb_raster.shape}, "
        f"Green Belt pixels: {gb_pixels} of {total_pixels} ({gb_pct:.2f}%)"
    )

    return gb_raster, profile


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD GREEN BELT RASTER")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "gb_eng": snk.input.gb_eng,
            "gb_sco": snk.input.gb_sco,
        }
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        input_paths = {
            "gb_eng": "data/land/societal/green_belt_england.gpkg",
            "gb_sco": "data/land/societal/green_belt_scotland.gpkg",
        }
        output_raster = "resources/land/green_belt_gb.tif"
        resolution = 100
        target_crs = 27700

    crs_str = f"EPSG:{target_crs}"
    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 0: Validate inputs
    stage_start = time.time()
    validate_inputs(input_paths)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 1-2: Load all Green Belt data
    stage_start = time.time()
    all_gb = load_all_green_belt_data(
        input_paths=input_paths,
        target_crs=crs_str,
    )
    stage_times["Load Green Belt data"] = time.time() - stage_start

    # Stages 3-5: Dissolve, grid, rasterize
    stage_start = time.time()
    gb_raster, profile = build_green_belt_raster(
        all_gb=all_gb,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 6: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(gb_raster, profile, output_raster)
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Green Belt Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "raster_shape": str(gb_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD GREEN BELT RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
