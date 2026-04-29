#!/usr/bin/env python3
"""
Build Scotland Mask Raster and Zone List.

Creates a binary raster mask identifying Scottish zones and a companion CSV
listing Scottish zone identifiers. Used by the nuclear eligibility pipeline
to enforce Scotland's ban on new nuclear power stations (both large nuclear
and SMR).

Zone shapes are loaded for the given network model (ETYS/Reduced → GSP
regions, Zonal → zonal zones). Scottish zones are identified by matching
zone names against the ``scotland.zones`` list in ``config/defaults.yaml``
(default: ["Z1_1", "Z1_2", "Z1_3", "Z1_4", "Z4"] — the zonal network
IDs covering North and South Scotland).

The raster mask is produced at 100m resolution in EPSG:27700 on the
canonical GB reference grid shared by all foundation rasters. Pixels within
Scottish zone boundaries are burned as 1; all other pixels are 0.

Uses ``{network_model}`` wildcard (not ``{scenario}``) because zone shapes
depend only on network_model. Multiple scenarios sharing the same
network_model reuse the same Scotland mask — no redundant computation.

Processing stages:
    1. Load zone shapes for the specified network model, validate CRS
    2. Identify Scottish zones by matching against config zone names
    3. Create canonical GB reference grid (100m, EPSG:27700)
    4. Rasterize Scottish zone geometries as binary mask
    5. Write outputs: single-band GeoTIFF + CSV of Scottish zone names

Input:
    - Zone shapes file (geojson) for the given network model, resolved
      via ``get_zones_for_model()`` in land_constraints.smk.

Output:
    - resources/land/scotland_mask_{network_model}.tif — single-band
      GeoTIFF (uint8, EPSG:27700, 100m resolution). Binary mask:
      1 = Scotland, 0 = not Scotland.
    - resources/land/scotland_zones_{network_model}.csv — CSV listing
      the Scottish zone identifiers (one column: ``zone``).

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
    load_zone_shapes,
    rasterize_vector,
    validate_crs,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_scotland_mask"
logger = setup_logging(log_path)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def load_and_filter_scottish_zones(
    zones_path: str,
    scottish_zone_names: list[str],
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Load zone shapes and filter to Scottish zones only.

    Loads the zone shapefile for the specified network model, validates
    CRS is EPSG:27700 after reprojection, and splits into Scottish and
    non-Scottish zones based on the config zone name list.

    Parameters
    ----------
    zones_path : str
        Path to zone shapes file (e.g. data/network/zonal/zones.geojson).
    scottish_zone_names : list of str
        Zone identifiers for Scotland from config ``scotland.zones``
        (e.g. ["Z1_1", "Z1_2", "Z1_3", "Z1_4", "Z4"]).

    Returns
    -------
    tuple of (gpd.GeoDataFrame, gpd.GeoDataFrame)
        ``(all_zones, scottish_zones)`` — both reprojected to EPSG:27700.
        ``scottish_zones`` contains only rows matching ``scottish_zone_names``.

    Raises
    ------
    ValueError
        If no Scottish zones are found matching the config names, indicating
        a mismatch between config and zone shapefile.
    """
    # Load and reproject to EPSG:27700
    all_zones = load_zone_shapes(zones_path, target_crs="EPSG:27700")
    validate_crs(all_zones, expected_crs="EPSG:27700")

    n_zones = len(all_zones)
    zone_names = all_zones["zone_name"].tolist()
    logger.info(f"Loaded {n_zones} zones: {zone_names}")

    # Filter to Scottish zones
    scottish_zones = all_zones[all_zones["zone_name"].isin(scottish_zone_names)].copy()

    matched = scottish_zones["zone_name"].tolist()
    unmatched = [z for z in scottish_zone_names if z not in zone_names]

    if len(scottish_zones) == 0:
        raise ValueError(
            f"No Scottish zones found. Config names {scottish_zone_names} "
            f"do not match any zone in {zones_path}. "
            f"Available zones: {zone_names}"
        )

    if unmatched:
        logger.warning(
            f"Config lists Scottish zones not found in shapefile: {unmatched}. "
            f"Proceeding with {len(matched)} matched zones: {matched}"
        )

    logger.info(f"Identified {len(scottish_zones)} Scottish zones: {matched}")

    return all_zones, scottish_zones


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_scotland_mask(
    scottish_zones: gpd.GeoDataFrame,
    resolution: int,
    target_crs: str = "EPSG:27700",
) -> tuple[np.ndarray, dict]:
    """
    Create canonical reference grid and rasterize Scottish zones as binary mask.

    Uses the canonical GB reference grid (shared by all foundation rasters)
    to ensure pixel alignment with other land constraint layers.

    Parameters
    ----------
    scottish_zones : gpd.GeoDataFrame
        Scottish zone geometries filtered by :func:`load_and_filter_scottish_zones`.
        Must be in EPSG:27700.
    resolution : int
        Raster resolution in metres (typically 100).
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(mask_raster, profile)`` where *mask_raster* has shape
        ``(height, width)`` with uint8 values (1 = Scotland, 0 = not Scotland),
        and *profile* is a rasterio profile dict.
    """
    # Create canonical GB reference grid
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )
    template = (width, height, transform, crs)

    # Rasterize Scottish zones as binary mask
    logger.info("Rasterizing Scottish zone geometries as binary mask...")
    mask_raster = rasterize_vector(
        scottish_zones,
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
    sco_pixels = np.count_nonzero(mask_raster)
    total_pixels = mask_raster.size
    sco_pct = 100.0 * sco_pixels / total_pixels if total_pixels > 0 else 0.0
    logger.info(
        f"Scotland mask: {mask_raster.shape}, "
        f"Scotland pixels: {sco_pixels} of {total_pixels} ({sco_pct:.2f}%)"
    )

    return mask_raster, profile


def write_scotland_zones_csv(
    scottish_zones: gpd.GeoDataFrame,
    output_path: str,
) -> None:
    """
    Write CSV listing Scottish zone identifiers.

    Parameters
    ----------
    scottish_zones : gpd.GeoDataFrame
        Scottish zone geometries with ``zone_name`` column.
    output_path : str
        Path for the output CSV file.
    """
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    zones_df = pd.DataFrame({"zone": sorted(scottish_zones["zone_name"].tolist())})
    zones_df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(zones_df)} Scottish zones to {output_path}")


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD SCOTLAND MASK")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        zones_path = snk.input.zones
        output_raster = snk.output.mask
        output_csv = snk.output.scotland_zones
        scottish_zone_names = snk.params.scottish_zones
        resolution = snk.params.resolution
        target_crs = f"EPSG:{snk.params.target_crs}"
    else:
        # Standalone defaults for testing (zonal network model)
        zones_path = "data/network/zonal/zones.geojson"
        output_raster = "resources/land/scotland_mask_zonal.tif"
        output_csv = "resources/land/scotland_zones_zonal.csv"
        scottish_zone_names = ["Z1_1", "Z1_2", "Z1_3", "Z1_4", "Z2", "Z3", "Z4", "Z5", "Z6"]
        resolution = 100
        target_crs = "EPSG:27700"

    logger.info(f"Zones path: {zones_path}")
    logger.info(f"Scottish zones from config: {scottish_zone_names}")
    logger.info(f"Resolution: {resolution}m, CRS: {target_crs}")

    # Stage 1-2: Load zone shapes and filter to Scottish zones
    stage_start = time.time()
    all_zones, scottish_zones = load_and_filter_scottish_zones(
        zones_path=zones_path,
        scottish_zone_names=scottish_zone_names,
    )
    stage_times["Load and filter zones"] = time.time() - stage_start

    # Stages 3-4: Create reference grid and rasterize
    stage_start = time.time()
    mask_raster, profile = build_scotland_mask(
        scottish_zones=scottish_zones,
        resolution=resolution,
        target_crs=target_crs,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 5a: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(
        mask_raster,
        profile,
        output_raster,
        band_names=["scotland_mask"],
    )
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Stage 5b: Write Scottish zones CSV
    stage_start = time.time()
    write_scotland_zones_csv(scottish_zones, output_csv)
    stage_times["Write CSV"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Scotland Mask",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "scottish_zones": scottish_zones["zone_name"].tolist(),
            "raster_shape": str(mask_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD SCOTLAND MASK — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
