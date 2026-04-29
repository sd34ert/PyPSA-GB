#!/usr/bin/env python3
"""
Filter SMR availability by minimum contiguous site area.

Reads the pixel-level exclusion raster produced by
build_nuclear_smr_exclusions, inverts it to get eligible pixels, then
labels contiguous regions using 8-connectivity. Regions smaller than
min_site_area (default 1 km² = 100 pixels at 100 m resolution) are
removed. The filtered result is written as both a raster (for
validation/reporting) and a per-zone availability CSV.

Input:
    - resources/land/smr_exclusions_{network_model}.tif — binary exclusion
      raster (0=available, 1=excluded) from build_nuclear_smr_exclusions
    - Zone shapes (for per-zone fraction calculation)

Output:
    - resources/land/smr_site_filtered_{network_model}.tif — binary raster
      (1=eligible site >= min_site_area, 0=excluded or too small)
    - resources/land/smr_availability_{network_model}.csv — per-zone
      availability fractions (0.0-1.0) from filtered eligible pixels

Author: K O'Neill
Date: 2026-03-30
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from scipy.ndimage import label

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
    load_zone_shapes,
    write_geotiff,
)

# ── Snakemake integration ────────────────────────────────────────────────────

snk = globals().get("snakemake")

log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "filter_smr_site_area"
)
logger = setup_logging(log_path)


# ── Helper functions ─────────────────────────────────────────────────────────


def filter_contiguous_areas(available, min_area_km2, resolution_m):
    """Remove eligible pixel clusters smaller than min_area_km2.

    Labels connected components in the binary ``available`` array using
    8-connectivity and removes regions whose total area is below the
    minimum threshold.

    Parameters
    ----------
    available : np.ndarray
        2-D binary array (1=eligible, 0=excluded), dtype uint8.
    min_area_km2 : float
        Minimum contiguous site area in km². Regions smaller than this
        are set to 0 (ineligible). If <= 0, no filtering is applied.
    resolution_m : float
        Raster resolution in metres per pixel.

    Returns
    -------
    np.ndarray
        Filtered binary array (uint8). Same shape as input.
    n_total : int
        Total number of contiguous regions found before filtering.
    n_valid : int
        Number of regions passing the minimum area threshold.
    """
    pixel_area_m2 = resolution_m ** 2
    min_pixels = int(min_area_km2 * 1e6 / pixel_area_m2)

    if min_pixels <= 1:
        # Every eligible pixel passes — count regions for logging only
        labelled, n_features = label(
            available, structure=np.ones((3, 3), dtype=int)
        )
        return available.copy(), n_features, n_features

    labelled, n_features = label(
        available, structure=np.ones((3, 3), dtype=int)
    )

    if n_features == 0:
        return available.copy(), 0, 0

    region_ids, region_sizes = np.unique(
        labelled[labelled > 0], return_counts=True
    )
    valid_ids = region_ids[region_sizes >= min_pixels]
    filtered = np.isin(labelled, valid_ids).astype(np.uint8)

    return filtered, n_features, len(valid_ids)


# ── Main processing ──────────────────────────────────────────────────────────


def main():
    """Filter SMR exclusion raster by minimum contiguous site area."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("FILTER SMR SITE AREA")
    logger.info("=" * 80)

    # ── Parse parameters ─────────────────────────────────────────────────
    if snk:
        exclusion_raster_path = snk.input.exclusion_raster
        zones_path = snk.input.zones
        output_raster = snk.output.filtered_raster
        output_csv = snk.output.availability_csv
        min_site_area = snk.params.min_site_area
        resolution_m = snk.params.resolution
        zone_threshold = snk.params.zone_threshold
    else:
        # Standalone fallback for testing
        exclusion_raster_path = "resources/land/smr_exclusions_Zonal.tif"
        zones_path = "data/network/zonal/zones.geojson"
        output_raster = "resources/land/smr_site_filtered_Zonal.tif"
        output_csv = "resources/land/smr_availability_Zonal.csv"
        min_site_area = 1.0
        resolution_m = 100
        zone_threshold = 0.01

    logger.info(f"Min site area: {min_site_area} km²")
    logger.info(f"Resolution: {resolution_m} m")
    min_pixels = int(min_site_area * 1e6 / (resolution_m ** 2))
    logger.info(f"Min contiguous pixels: {min_pixels}")
    logger.info(f"Zone threshold: {zone_threshold} (zones below set to 0)")

    # ── Stage 1: Load exclusion raster ───────────────────────────────────
    stage_start = time.time()
    exclusion_path = Path(exclusion_raster_path)
    if not exclusion_path.exists():
        raise FileNotFoundError(
            f"FAILED: exclusion raster not found: {exclusion_path}"
        )

    with rasterio.open(exclusion_path) as src:
        exclusion = src.read(1)
        transform = src.transform
        crs = src.crs
        height, width = exclusion.shape

    logger.info(f"Loaded exclusion raster: {width}x{height}, CRS={crs}")
    stage_times["Load raster"] = time.time() - stage_start

    # ── Stage 2: Load zone shapes ────────────────────────────────────────
    stage_start = time.time()
    zones = load_zone_shapes(zones_path, target_crs=crs.to_epsg())

    # Filter out offshore zones (same as build_nuclear_smr_exclusions)
    offshore = ["DOGGER_BANK", "HORNSEA", "EAST_ANGLIA"]
    zones = zones[~zones["zone_name"].isin(offshore)].copy()
    logger.info(f"Loaded {len(zones)} onshore zones")
    stage_times["Load zones"] = time.time() - stage_start

    # ── Stage 3: Filter contiguous areas ─────────────────────────────────
    stage_start = time.time()
    available = (~exclusion.astype(bool)).astype(np.uint8)

    pixels_before = int(available.sum())
    filtered, n_total, n_valid = filter_contiguous_areas(
        available, min_site_area, resolution_m
    )
    pixels_after = int(filtered.sum())
    pixels_removed = pixels_before - pixels_after

    logger.info(f"Contiguous regions found: {n_total}")
    logger.info(f"Regions >= {min_site_area} km²: {n_valid}")
    logger.info(
        f"Pixels: {pixels_before} eligible → {pixels_after} after filtering "
        f"({pixels_removed} removed, "
        f"{100.0 * pixels_removed / max(pixels_before, 1):.1f}%)"
    )
    stage_times["Filter contiguous"] = time.time() - stage_start

    # ── Stage 4: Write filtered raster ───────────────────────────────────
    stage_start = time.time()
    profile = {
        "width": width,
        "height": height,
        "crs": crs,
        "transform": transform,
        "dtype": "uint8",
    }
    write_geotiff(
        array=filtered,
        profile=profile,
        path=output_raster,
        band_names=["smr_site_filtered"],
        nodata=255,
    )
    logger.info(f"Filtered raster written: {output_raster}")
    stage_times["Write raster"] = time.time() - stage_start

    # ── Stage 5: Calculate per-zone availability and write CSV ───────────
    stage_start = time.time()
    zone_availability = calculate_zone_fraction(filtered, zones, transform)

    # Compute zone areas from geometries (CRS is EPSG:27700, units=metres)
    zone_areas = zones.set_index("zone_name")["geometry"].area / 1e6

    fracs = zone_availability.values.clip(0.0, 1.0)

    # Apply zone threshold: zones with fraction below threshold set to zero
    below_threshold = fracs < zone_threshold
    n_zeroed = int(below_threshold.sum())
    if n_zeroed > 0:
        zeroed_zones = zone_availability.index[below_threshold].tolist()
        fracs[below_threshold] = 0.0
        logger.info(
            f"Zone threshold ({zone_threshold}): {n_zeroed} zone(s) set to 0 "
            f"— {zeroed_zones}"
        )

    availability_df = pd.DataFrame({
        "zone": zone_availability.index,
        "area_km2": zone_areas.reindex(zone_availability.index).values,
        "smr_available_frac": fracs,
    })

    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    availability_df.to_csv(output_csv, index=False)
    logger.info(f"Availability CSV written: {output_csv}")
    logger.info(f"\n{availability_df.to_string(index=False)}")
    stage_times["Write CSV"] = time.time() - stage_start

    # ── Log summary ──────────────────────────────────────────────────────
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Filter SMR Site Area",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "min_site_area_km2": min_site_area,
            "resolution_m": resolution_m,
            "min_pixels": min_pixels,
            "regions_total": n_total,
            "regions_valid": n_valid,
            "pixels_removed": pixels_removed,
            "zone_threshold": zone_threshold,
            "zones_zeroed": n_zeroed,
            "zones": len(zones),
        },
    )

    logger.info("=" * 80)
    logger.info("DONE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
