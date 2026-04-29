#!/usr/bin/env python3
"""
Build Groundwater Protection Zone Raster for England and Wales.

Creates a 3-band raster from Source Protection Zone (SPZ) data for England
and Wales. Band 1 contains SPZ1 (inner protection zones closest to the
abstraction point), Band 2 contains SPZ2 (outer protection zones),
Band 3 contains SPZ3 (total catchment zones). Scotland does not
implement similar protection areas around drinking water sources and
is excluded.


The SPZ ``number`` column classifies features into bands:
- Band 1 (SPZ1): number starting with '1' (includes '1' and '1c')
- Band 2 (SPZ2): number starting with '2' (includes '2' and '2c')
- Band 3 (SPZ3): number starting with '3' (includes '3' and '3c')
- Dropped: number '4' (zone of special interest — no formal protection,
  not referenced by any exclusion_zones config parameter)

Each band is dissolved independently and rasterized onto the canonical GB
reference grid shared by all foundation rasters. Downstream consumers
(calculate_zone_statistics, build_availability_matrix) select bands via the
per-technology ``exclusion_zones`` config parameter.

Processing stages:
    1. Load Defra SPZ data (England) preserving zone number
    2. Load NRW SPZ data (Wales) preserving zone number
    3. Merge into single GeoDataFrame
    4. Classify features into SPZ1, SPZ2, and SPZ3 subsets
    5. Dissolve each subset independently
    6. Create canonical GB reference grid
    7. Rasterize each subset as a separate band
    8. Stack into 3-band array and write GeoTIFF

Input:
    - data/land/environment/source_protection_zones_england.gpkg — Defra/EA
      Source Protection Zones for England. Source: data.gov.uk.
    - data/land/environment/source_protection_zones_wales.gpkg — NRW Source
      Protection Zones for Wales. Source: Natural Resources Wales.

Output:
    - resources/land/groundwater_spz_ew.tif — 3-band GeoTIFF (uint8, EPSG:27700,
      100m resolution). Band 1 = SPZ1, Band 2 = SPZ2, Band 3 = SPZ3.
      Values: 1 = zone present, 0 = absent.

Author: K O'Neill
Date: 2026-03-04
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
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_groundwater_protection"
logger = setup_logging(log_path)

# Band names for the 3-band output GeoTIFF
BAND_NAMES = ["SPZ1", "SPZ2", "SPZ3"]


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


def classify_spz_zones(
    gdf: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Split merged SPZ data into SPZ1, SPZ2, and SPZ3 subsets.

    Handles dtype mismatch between England (object/'str') and Wales
    (int16) by casting the ``number`` column to string before
    classification.

    Classification:
    - Band 1 (SPZ1): number starting with '1' (captures '1' and '1c')
    - Band 2 (SPZ2): number starting with '2' (captures '2' and '2c')
    - Band 3 (SPZ3): number starting with '3' (captures '3' and '3c')
    - Dropped: number '4' (zone of special interest, no formal
      protection)

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Merged SPZ data with ``number``, ``geometry``, and
        ``data_source`` columns. CRS must already be set.

    Returns
    -------
    tuple of (gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame)
        ``(spz1_gdf, spz2_gdf, spz3_gdf)`` — subsets for Bands 1–3.
    """
    # Cast to string to handle mixed dtypes (England=object, Wales=int16)
    zone_str = gdf["number"].astype(str)

    mask_spz1 = zone_str.str.startswith("1")
    mask_spz2 = zone_str.str.startswith("2")
    mask_spz3 = zone_str.str.startswith("3")

    spz1_gdf = gdf.loc[mask_spz1].copy()
    spz2_gdf = gdf.loc[mask_spz2].copy()
    spz3_gdf = gdf.loc[mask_spz3].copy()

    n_classified = len(spz1_gdf) + len(spz2_gdf) + len(spz3_gdf)
    n_dropped = len(gdf) - n_classified

    logger.info("SPZ zone classification:")
    logger.info(f"  Band 1 (SPZ1): {len(spz1_gdf)} features")
    logger.info(f"  Band 2 (SPZ2): {len(spz2_gdf)} features")
    logger.info(f"  Band 3 (SPZ3): {len(spz3_gdf)} features")
    if n_dropped > 0:
        logger.info(
            f"  Dropped (zone 4 / unclassified): {n_dropped} features"
        )

    return spz1_gdf, spz2_gdf, spz3_gdf


def load_ew_spz_data(
    input_paths: dict[str, str],
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load and merge England and Wales SPZ datasets, preserving zone number.

    Loads two SPZ datasets (Defra/EA for England, NRW for Wales), reprojects
    each to the target CRS, and concatenates into a single GeoDataFrame.
    Each feature is tagged with its data source for provenance. The
    ``number`` column is preserved for downstream zone classification.

    Parameters
    ----------
    input_paths : dict of str to str
        Mapping of input names to file paths. Expected keys:
        ``spz_eng``, ``spz_wal``.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Merged SPZ geometries with columns: number, geometry, data_source.
        CRS is ``target_crs``. Not yet dissolved — overlapping geometries
        from different sources may exist.
    """
    datasets = []

    # England — Defra/EA Source Protection Zones
    logger.info(f"Loading Defra SPZ from {Path(input_paths['spz_eng']).name}")
    spz_eng = load_and_reproject_vector(input_paths["spz_eng"], target_crs=target_crs)
    logger.info(f"  Defra SPZ: {len(spz_eng)} features")
    spz_eng["data_source"] = "Defra_EA"
    datasets.append(spz_eng[["number", "geometry", "data_source"]])

    # Wales — NRW Source Protection Zones
    logger.info(f"Loading NRW SPZ from {Path(input_paths['spz_wal']).name}")
    spz_wal = load_and_reproject_vector(input_paths["spz_wal"], target_crs=target_crs)
    logger.info(f"  NRW SPZ: {len(spz_wal)} features")
    spz_wal["data_source"] = "NRW"
    datasets.append(spz_wal[["number", "geometry", "data_source"]])

    # Merge into single GeoDataFrame
    all_spz = pd.concat(datasets, ignore_index=True)
    all_spz = all_spz.set_crs(target_crs)
    total_features = len(all_spz)
    logger.info(f"E&W SPZ data merged: {total_features} features from 2 datasets")

    # Log per-source counts
    source_counts = all_spz["data_source"].value_counts()
    for source, count in source_counts.items():
        logger.info(f"  {source}: {count} features")

    return all_spz


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_groundwater_spz_raster(
    all_spz: gpd.GeoDataFrame,
    resolution: int,
    target_crs: str = "EPSG:27700",
) -> tuple[np.ndarray, dict]:
    """
    Classify, dissolve, and rasterize SPZ data into a 3-band mask.

    Takes the merged (but not yet dissolved) SPZ GeoDataFrame from
    :func:`load_ew_spz_data`, classifies features into SPZ1, SPZ2,
    and SPZ3, dissolves each subset independently, creates the
    canonical GB reference grid, and rasterizes each as a separate
    band.

    Parameters
    ----------
    all_spz : gpd.GeoDataFrame
        Merged SPZ geometries from England and Wales with ``number``,
        ``geometry``, and ``data_source`` columns. CRS must match
        ``target_crs``.
    resolution : int
        Raster resolution in metres.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(spz_raster, profile)`` where *spz_raster* has shape
        ``(3, height, width)`` with uint8 values (1 = zone present,
        0 = absent). Band 1 = SPZ1, Band 2 = SPZ2, Band 3 = SPZ3.
        *profile* is a rasterio profile dict with ``count=3``.
    """
    # Classify into SPZ1, SPZ2, SPZ3
    spz1_gdf, spz2_gdf, spz3_gdf = classify_spz_zones(all_spz)

    # Dissolve each subset independently
    logger.info("Dissolving SPZ1 geometries...")
    spz1_dissolved = dissolve_overlaps(spz1_gdf)
    logger.info(
        f"  SPZ1: {len(spz1_gdf)} -> {len(spz1_dissolved)} dissolved"
    )

    logger.info("Dissolving SPZ2 geometries...")
    spz2_dissolved = dissolve_overlaps(spz2_gdf)
    logger.info(
        f"  SPZ2: {len(spz2_gdf)} -> {len(spz2_dissolved)} dissolved"
    )

    logger.info("Dissolving SPZ3 geometries...")
    spz3_dissolved = dissolve_overlaps(spz3_gdf)
    logger.info(
        f"  SPZ3: {len(spz3_gdf)} -> {len(spz3_dissolved)} dissolved"
    )

    # Validate coverage — E&W only, so cannot meet 90% GB threshold
    validate_gb_coverage(all_spz, min_fraction=0.0)

    # Create canonical GB reference grid
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )
    template = (width, height, transform, crs)

    # Rasterize each band
    logger.info("Rasterizing Band 1 (SPZ1)...")
    band1 = rasterize_vector(
        spz1_dissolved, template, burn_value=1, dtype="uint8"
    )

    logger.info("Rasterizing Band 2 (SPZ2)...")
    band2 = rasterize_vector(
        spz2_dissolved, template, burn_value=1, dtype="uint8"
    )

    logger.info("Rasterizing Band 3 (SPZ3)...")
    band3 = rasterize_vector(
        spz3_dissolved, template, burn_value=1, dtype="uint8"
    )

    # Stack into 3-band array: shape (3, height, width)
    spz_raster = np.stack([band1, band2, band3], axis=0)

    # Build rasterio profile
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 3,
        "crs": target_crs,
        "transform": transform,
    }

    # Log per-band summary statistics
    total_pixels = band1.size
    bands = [band1, band2, band3]
    for band, name in zip(bands, BAND_NAMES):
        n_pixels = np.count_nonzero(band)
        pct = (100.0 * n_pixels / total_pixels
               if total_pixels > 0 else 0.0)
        logger.info(f"  {name}: {n_pixels} pixels ({pct:.2f}%)")

    return spz_raster, profile


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD DRINKING WATER PROTECTION RASTER (E&W, 3-BAND)")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "spz_eng": snk.input.spz_eng,
            "spz_wal": snk.input.spz_wal,
        }
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        # Standalone defaults for testing
        input_paths = {
            "spz_eng": "data/land/environment/source_protection_zones_england.gpkg",
            "spz_wal": "data/land/environment/source_protection_zones_wales.gpkg",
        }
        output_raster = "resources/land/groundwater_spz_ew.tif"
        resolution = 100
        target_crs = 27700

    # Convert integer CRS code to EPSG string
    crs_str = f"EPSG:{target_crs}"

    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 1: Validate inputs
    stage_start = time.time()
    validate_inputs(input_paths)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Load England & Wales SPZ data
    stage_start = time.time()
    all_spz = load_ew_spz_data(input_paths, target_crs=crs_str)
    stage_times["Load E&W SPZ data"] = time.time() - stage_start

    # Stage 3: Build 3-band raster
    stage_start = time.time()
    spz_raster, profile = build_groundwater_spz_raster(
        all_spz,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Build 3-band raster"] = time.time() - stage_start

    # Stage 4: Write GeoTIFF
    stage_start = time.time()
    Path(output_raster).parent.mkdir(parents=True, exist_ok=True)
    write_geotiff(
        spz_raster,
        profile,
        output_raster,
        band_names=BAND_NAMES,
    )
    logger.info(f"Wrote 3-band SPZ raster to {output_raster}")
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Drinking Water Protection Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": crs_str,
            "raster_shape": str(spz_raster.shape),
            "coverage": "England & Wales only",
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD DRINKING WATER PROTECTION RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
