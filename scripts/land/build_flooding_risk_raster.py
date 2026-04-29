#!/usr/bin/env python3
"""
Build Flooding Risk Raster for Great Britain.

Merges Environment Agency (England), Natural Resources Wales, and SEPA
(Scotland) flood zone datasets into a unified binary flood risk raster at
100m resolution in EPSG:27700.

England's EA Flood Zones dataset contains Flood Zone 2 (medium probability)
and Flood Zone 3 (high probability) extents for rivers and the sea.
Scotland's SEPA provides separate datasets for river, surface water, and
coastal flooding (medium likelihood extents). Wales's NRW provides rivers &
seas flood zones and a separate surface water flood map.

Each input file is processed in chunks of features using fiona, so that
memory usage is bounded by the chunk size (~10 000 features) rather than the
full file (~5 GB for England alone). Since the output is binary (1 = flood,
0 = no flood), overlapping polygons are handled idempotently: each chunk is
rasterized and OR-merged into an accumulator array.

Technology branches apply their own interpretation of flood risk (hard
exclusion for onwind/solar, threshold-based for nuclear/hydrogen via
zone_statistics).

Processing stages:
    1. Validate all input files exist
    2. Create canonical GB reference grid (shared across all foundation rasters)
    3. For each flood zone file: stream features in chunks via fiona,
       reproject if needed, rasterize each chunk, OR-merge into accumulator
    4. Write output: single-band GeoTIFF

Input:
    - data/land/hazards/flood_zones_cc_england.gpkg — EA Flood Zones
      with Climate Change for England (rivers and sea). Source: data.gov.uk.
    - data/land/hazards/flood_zones_rivers_scotland.gpkg — SEPA fluvial
      (river) flood extent, medium likelihood. Source: SEPA Flood Maps.
    - data/land/hazards/flood_zones_surface_scotland.gpkg — SEPA surface
      water flood extent, medium likelihood. Source: SEPA Flood Maps.
    - data/land/hazards/flood_zones_coastal_scotland.gpkg — SEPA coastal
      flood extent, medium likelihood. Source: SEPA Flood Maps.
    - data/land/hazards/flood_zones_seas_rivers_wales.gpkg — NRW flood
      zones for Wales (rivers and sea). Source: Natural Resources Wales.
    - data/land/hazards/flood_zones_surface_wales.gpkg — NRW surface
      water flood map for Wales. Source: Natural Resources Wales.

Output:
    - resources/land/flooding_risk_gb.tif — single-band GeoTIFF (uint8,
      EPSG:27700, 100m resolution). Binary mask: 1 = flood risk area,
      0 = no flood risk.

Author: K O'Neill
Date: 2026-03-02
"""

import gc
import logging
import sys
import time
from pathlib import Path

import fiona
import numpy as np
from pyproj import CRS, Transformer
from rasterio import features
from shapely.geometry import shape
from shapely.ops import transform

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
    validate_gb_coverage,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_flooding_risk_raster"
logger = setup_logging(log_path)

# Chunk size for streaming features from GPKG files
CHUNK_SIZE = 10_000


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


def _rasterize_file_chunked(
    filepath: str,
    accumulator: np.ndarray,
    out_transform,
    target_crs: str = "EPSG:27700",
    chunk_size: int = CHUNK_SIZE,
) -> tuple[int, tuple[float, float, float, float]]:
    """
    Stream features from a vector file in chunks, rasterize, and OR-merge.

    Reads features from the file using fiona in batches of *chunk_size*,
    reprojects to *target_crs* if needed, rasterizes each batch, and
    OR-merges into *accumulator* in place. This keeps peak memory bounded
    by the chunk size rather than the total file size.

    Parameters
    ----------
    filepath : str
        Path to vector file (GeoPackage, Shapefile, etc.).
    accumulator : np.ndarray
        2-D uint8 array to OR-merge rasterized chunks into (modified in place).
    out_transform : rasterio.Affine
        Affine transform for the output raster grid.
    target_crs : str, optional
        Target CRS string, by default "EPSG:27700".
    chunk_size : int, optional
        Number of features to process per batch, by default 10 000.

    Returns
    -------
    tuple of (int, tuple of float)
        ``(feature_count, (xmin, ymin, xmax, ymax))`` — total features
        processed and the cumulative bounding box in target CRS units.
    """
    height, width = accumulator.shape
    feature_count = 0
    bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    with fiona.open(filepath) as src:
        src_crs = CRS.from_user_input(src.crs)
        target = CRS.from_user_input(target_crs)
        needs_reproject = src_crs != target

        if needs_reproject:
            transformer = Transformer.from_crs(src_crs, target, always_xy=True)
            reproject_fn = transformer.transform
            logger.info(f"  Reprojecting from {src_crs} to {target_crs}")

        chunk = []
        for feat in src:
            geom = shape(feat["geometry"])
            if geom.is_empty or geom is None:
                continue

            if needs_reproject:
                geom = transform(reproject_fn, geom)

            chunk.append(geom)
            feature_count += 1

            if len(chunk) >= chunk_size:
                # Rasterize this chunk directly into a temporary array
                shapes = [(g, 1) for g in chunk]
                burned = features.rasterize(
                    shapes=shapes,
                    out_shape=(height, width),
                    transform=out_transform,
                    fill=0,
                    dtype="uint8",
                )
                np.maximum(accumulator, burned, out=accumulator)

                # Update bounds from chunk geometries
                for g in chunk:
                    gb = g.bounds
                    bounds[0] = min(bounds[0], gb[0])
                    bounds[1] = min(bounds[1], gb[1])
                    bounds[2] = max(bounds[2], gb[2])
                    bounds[3] = max(bounds[3], gb[3])

                del chunk, shapes, burned
                gc.collect()
                chunk = []

        # Process remaining features
        if chunk:
            shapes = [(g, 1) for g in chunk]
            burned = features.rasterize(
                shapes=shapes,
                out_shape=(height, width),
                transform=out_transform,
                fill=0,
                dtype="uint8",
            )
            np.maximum(accumulator, burned, out=accumulator)

            for g in chunk:
                gb = g.bounds
                bounds[0] = min(bounds[0], gb[0])
                bounds[1] = min(bounds[1], gb[1])
                bounds[2] = max(bounds[2], gb[2])
                bounds[3] = max(bounds[3], gb[3])

            del chunk, shapes, burned
            gc.collect()

    return feature_count, tuple(bounds)


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_flooding_risk_raster(
    input_paths: dict[str, str],
    resolution: int,
    target_crs: str = "EPSG:27700",
) -> tuple[np.ndarray, dict]:
    """
    Stream flood zone features in chunks, rasterize, and OR-merge.

    Processes each input file by streaming features via fiona in batches,
    keeping peak memory bounded by the chunk size (~10 000 features) rather
    than the full file size. Since the output is binary, overlapping flood
    zones are handled idempotently via ``np.maximum``.

    Parameters
    ----------
    input_paths : dict of str to str
        Mapping of input names to file paths for the 6 flood zone datasets.
    resolution : int
        Raster resolution in metres.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(flood_raster, profile)`` where *flood_raster* has shape
        ``(height, width)`` with uint8 values (1 = flood risk, 0 = none),
        and *profile* is a rasterio profile dict.
    """
    # Create canonical GB reference grid (once)
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )

    # Initialise accumulator raster (all zeros)
    flood_raster = np.zeros((height, width), dtype="uint8")

    # Track cumulative bounding box for GB coverage validation
    cumulative_bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    total_features = 0

    # Process each input file in chunks
    for name, path in input_paths.items():
        logger.info(f"Processing {name}: {Path(path).name}")

        n_features, file_bounds = _rasterize_file_chunked(
            filepath=path,
            accumulator=flood_raster,
            out_transform=transform,
            target_crs=target_crs,
        )

        total_features += n_features
        logger.info(f"  {name}: {n_features} features rasterized")

        # Update cumulative bounds
        if n_features > 0:
            cumulative_bounds[0] = min(cumulative_bounds[0], file_bounds[0])
            cumulative_bounds[1] = min(cumulative_bounds[1], file_bounds[1])
            cumulative_bounds[2] = max(cumulative_bounds[2], file_bounds[2])
            cumulative_bounds[3] = max(cumulative_bounds[3], file_bounds[3])

    # Validate cumulative GB coverage
    logger.info(f"Total features processed: {total_features} across {len(input_paths)} datasets")
    validate_gb_coverage(tuple(cumulative_bounds))

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
    flood_pixels = np.count_nonzero(flood_raster)
    total_pixels = flood_raster.size
    flood_pct = 100.0 * flood_pixels / total_pixels if total_pixels > 0 else 0.0
    logger.info(
        f"Flood risk raster: {flood_raster.shape}, "
        f"flood pixels: {flood_pixels} of {total_pixels} ({flood_pct:.2f}%)"
    )

    return flood_raster, profile


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD FLOODING RISK RASTER")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "flood_eng": snk.input.flood_eng,
            "flood_sco_river": snk.input.flood_sco_river,
            "flood_sco_surface": snk.input.flood_sco_surface,
            "flood_sco_coastal": snk.input.flood_sco_coastal,
            "flood_wal": snk.input.flood_wal,
            "flood_wal_surface": snk.input.flood_wal_surface,
        }
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        input_paths = {
            "flood_eng": "data/land/hazards/flood_zones_cc_england.gpkg",
            "flood_sco_river": "data/land/hazards/flood_zones_rivers_scotland.gpkg",
            "flood_sco_surface": "data/land/hazards/flood_zones_surface_scotland.gpkg",
            "flood_sco_coastal": "data/land/hazards/flood_zones_coastal_scotland.gpkg",
            "flood_wal": "data/land/hazards/flood_zones_seas_rivers_wales.gpkg",
            "flood_wal_surface": "data/land/hazards/flood_zones_surface_wales.gpkg",
        }
        output_raster = "resources/land/flooding_risk_gb.tif"
        resolution = 100
        target_crs = 27700

    crs_str = f"EPSG:{target_crs}"
    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 1: Validate inputs
    stage_start = time.time()
    validate_inputs(input_paths)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Build raster (chunked streaming per file)
    stage_start = time.time()
    flood_raster, profile = build_flooding_risk_raster(
        input_paths=input_paths,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 3: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(flood_raster, profile, output_raster)
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Flooding Risk Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "raster_shape": str(flood_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD FLOODING RISK RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
