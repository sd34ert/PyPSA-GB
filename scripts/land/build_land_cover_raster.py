#!/usr/bin/env python3
"""
Build Land Cover Raster for Great Britain.

Reprojects and resamples the UK Land Cover Map (LCM) 2024 GeoTIFF into an
analysis-ready raster at 100m resolution in EPSG:27700, aligned to the
canonical GB reference grid shared by all foundation rasters.

The UK LCM 2024 (UKCEH) is a 25m raster classifying the United Kingdom
into 21 target habitat classes. The source raster covers the whole UK
(including Northern Ireland) in EPSG:27700 (OSGB36 / British National
Grid) but at a finer resolution than needed. This script reprojects and
resamples to 100m using nearest-neighbour interpolation, then masks to
Great Britain only (England, Scotland, Wales) using dissolved GSP region
boundaries. Original integer class codes are retained for
technology-specific filtering downstream (e.g. onshore wind eligible on
codes 4, 5, 6; solar eligible on codes 3, 4).

UK LCM 2024 target habitat classes:
    1  — Broadleaved Woodland
    2  — Coniferous Woodland
    3  — Arable and Horticulture
    4  — Improved Grassland
    5  — Neutral Grassland
    6  — Calcareous Grassland
    7  — Acid Grassland
    8  — Fen, Marsh and Swamp
    9  — Heather
    10 — Heather Grassland
    11 — Bog
    12 — Inland Rock
    13 — Saltwater
    14 — Freshwater
    15 — Supra-littoral Rock
    16 — Supra-littoral Sediment
    17 — Littoral Rock
    18 — Littoral Sediment
    19 — Saltmarsh
    20 — Urban
    21 — Suburban

Processing stages:
    1. Load source raster (UK LCM 2024 GeoTIFF)
    2. Create canonical GB reference grid (shared across all foundation
       rasters)
    3. Reproject and resample to 100m using nearest-neighbour
       interpolation (preserves categorical values)
    4. Mask to Great Britain only using dissolved GSP region boundaries
       (removes Northern Ireland)
    5. Validate output class codes against expected LCM 2024 classes
    6. Write output: single-band GeoTIFF

Input:
    - data/land/environment/uk_lcm_2024.tif — UK Centre for Ecology &
      Hydrology (UKCEH) Land Cover Map 2024. Single-band raster with
      integer class codes 1–21. Source: UKCEH via EIDC.
    - data/network/GSP/GSP_regions_20250109/GSP_regions_20250109.shp —
      ESO Grid Supply Point regions covering GB only. Used dissolved as
      a mask to exclude Northern Ireland.

Output:
    - resources/land/land_cover_gb.tif — single-band GeoTIFF (uint8,
      EPSG:27700, 100m resolution). Pixel values are integer land cover
      class codes (1–21). nodata = 0.

Author: K O'Neill
Date: 2026-03-10
"""

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject

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
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_land_cover_raster"
logger = setup_logging(log_path)

# Expected LCM 2024 target habitat class codes (1–21) plus nodata (0)
LCM_VALID_CLASSES = set(range(0, 22))


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def validate_inputs(lcm_path: str) -> None:
    """
    Validate that the input LCM raster file exists and is readable.

    Parameters
    ----------
    lcm_path : str
        Path to the UK LCM 2024 GeoTIFF.

    Raises
    ------
    FileNotFoundError
        If the input file does not exist.
    """
    if not Path(lcm_path).exists():
        raise FileNotFoundError(f"LCM raster not found: {lcm_path}")

    logger.info(f"Input file validated: {lcm_path}")


def reproject_to_reference_grid(
    lcm_path: str,
    resolution: int,
    target_crs: str,
) -> tuple[np.ndarray, dict]:
    """
    Reproject LCM raster onto the canonical GB reference grid.

    Unlike ``land_utils.reproject_raster`` (which computes its own output
    grid via ``calculate_default_transform``), this function reprojects
    onto the exact canonical grid from ``create_reference_grid`` so the
    output is pixel-aligned with all other foundation rasters.

    Uses nearest-neighbour resampling to preserve categorical class codes.

    Parameters
    ----------
    lcm_path : str
        Path to the source UK LCM 2024 GeoTIFF.
    resolution : int
        Target raster resolution in metres.
    target_crs : str
        Target coordinate reference system (e.g. "EPSG:27700").

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(dst_array, dst_profile)`` where *dst_array* has shape
        ``(height, width)`` with uint8 class codes on the canonical grid,
        and *dst_profile* is a rasterio profile dict for writing.
    """
    # Create canonical GB reference grid (shared with all foundation rasters)
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )

    # Allocate output array
    dst_array = np.zeros((height, width), dtype="uint8")

    # Reproject source raster onto the canonical grid
    with rasterio.open(lcm_path) as src:
        logger.info(
            f"Source LCM raster: {src.width}x{src.height} pixels, "
            f"CRS={src.crs}, dtype={src.dtypes[0]}, "
            f"resolution=({src.res[0]:.1f}, {src.res[1]:.1f})m"
        )
        reproject(
            source=rasterio.band(src, 1),
            destination=dst_array,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs=target_crs,
            resampling=Resampling.nearest,
        )

    logger.info(
        f"Reprojected LCM to canonical grid: {width}x{height} pixels, "
        f"resolution={resolution}m, CRS={target_crs}"
    )

    # Build rasterio profile for output
    dst_profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": target_crs,
        "transform": transform,
    }

    return dst_array, dst_profile


def mask_to_gb(
    dst_array: np.ndarray,
    dst_profile: dict,
    gb_boundary_path: str,
) -> np.ndarray:
    """
    Mask raster to Great Britain using dissolved GSP region boundaries.

    Pixels outside the GB boundary (e.g. Northern Ireland) are set to
    nodata (0). The mask is built by dissolving all GSP regions into a
    single polygon, reprojecting to match the raster CRS if needed, and
    applying ``rasterio.features.geometry_mask``.

    Parameters
    ----------
    dst_array : np.ndarray
        Reprojected land cover raster (2D, uint8).
    dst_profile : dict
        Rasterio profile with 'transform' and 'crs' keys.
    gb_boundary_path : str
        Path to GB-only boundary shapefile (e.g. GSP regions).

    Returns
    -------
    np.ndarray
        Masked raster with Northern Ireland pixels set to 0.
    """
    gdf = gpd.read_file(gb_boundary_path)

    # Reproject to raster CRS if needed
    raster_crs = dst_profile["crs"]
    if gdf.crs and not gdf.crs.equals(raster_crs):
        gdf = gdf.to_crs(raster_crs)

    # Dissolve all regions into a single GB outline
    gb_outline = gdf.dissolve().geometry.values[0]

    # Create boolean mask: True where OUTSIDE GB
    outside_gb = geometry_mask(
        [gb_outline],
        out_shape=dst_array.shape,
        transform=dst_profile["transform"],
        invert=False,  # True = outside geometry
    )

    # Count pixels being masked
    ni_pixels = np.count_nonzero((outside_gb) & (dst_array > 0))
    logger.info(f"GB mask applied: {ni_pixels} non-GB land cover pixels set to nodata")

    masked = dst_array.copy()
    masked[outside_gb] = 0
    return masked


def validate_class_codes(dst_array: np.ndarray) -> None:
    """
    Validate that reprojected raster contains only expected LCM class codes.

    Parameters
    ----------
    dst_array : np.ndarray
        Reprojected land cover raster with uint8 class codes.

    Raises
    ------
    ValueError
        If unexpected class codes are found in the output raster.
    """
    unique_classes = set(np.unique(dst_array))
    unexpected = unique_classes - LCM_VALID_CLASSES

    if unexpected:
        raise ValueError(
            f"Unexpected land cover class codes in output: {sorted(unexpected)}. "
            f"Expected only {sorted(LCM_VALID_CLASSES)}."
        )

    # Log class distribution (excluding nodata=0)
    data_classes = unique_classes - {0}
    logger.info(f"Validated class codes: {sorted(data_classes)}")

    total_pixels = dst_array.size
    nodata_pixels = np.count_nonzero(dst_array == 0)
    data_pixels = total_pixels - nodata_pixels
    logger.info(
        f"Land cover pixels: {data_pixels} of {total_pixels} "
        f"({100.0 * data_pixels / total_pixels:.1f}%), "
        f"nodata: {nodata_pixels} ({100.0 * nodata_pixels / total_pixels:.1f}%)"
    )


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD LAND COVER RASTER")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        lcm_path = snk.input.lcm
        gb_boundary_path = snk.input.gb_boundary
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        lcm_path = "data/land/environment/uk_lcm_2024.tif"
        gb_boundary_path = "data/network/GSP/GSP_regions_20250109/GSP_regions_20250109.shp"
        output_raster = "resources/land/land_cover_gb.tif"
        resolution = 100
        target_crs = 27700

    crs_str = f"EPSG:{target_crs}"
    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 0: Validate inputs
    stage_start = time.time()
    validate_inputs(lcm_path)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 1: Create reference grid and reproject
    stage_start = time.time()
    dst_array, dst_profile = reproject_to_reference_grid(
        lcm_path=lcm_path,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Reproject to reference grid"] = time.time() - stage_start

    # Stage 2: Mask to Great Britain (remove Northern Ireland)
    stage_start = time.time()
    dst_array = mask_to_gb(dst_array, dst_profile, gb_boundary_path)
    stage_times["Mask to GB"] = time.time() - stage_start

    # Stage 3: Validate class codes
    stage_start = time.time()
    validate_class_codes(dst_array)
    stage_times["Validate class codes"] = time.time() - stage_start

    # Stage 4: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(dst_array, dst_profile, output_raster, nodata=0)
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Land Cover Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "raster_shape": str(dst_array.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD LAND COVER RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
