#!/usr/bin/env python3
"""
Process offshore wind KRAs: subtract exclusions, classify TG/cost tiers,
calculate distance-to-coast, intersect with zones, compute available area.

Loads Fixed and Floating Key Resource Area (KRA) polygons from The Crown
Estate, subtracts marine exclusion zones (MPAs, shipping, O&G, CCS, etc.)
using the offshore exclusion raster, and produces a GeoPackage where each
record is one KRA-zone intersection fragment with full classification
attributes preserved for downstream use.

Processing steps:
    1. Load and reproject Fixed + Floating KRA geojsons to EPSG:27700
    2. Parse TG (Technology Group) classification from Rating attribute
    3. Map TG → cost_tier and capex_multiplier via lookup tables
    4. Derive coastline from onshore zone boundaries, calculate
       centroid distance to nearest coastline point
    5. Classify carrier (offwind-fixed-ac/dc, offwind-float-ac/dc)
       and connection type (AC <50km, DC ≥50km) per KRA
    6. Intersect KRA polygons with zone boundaries (gpd.overlay)
    7. Calculate available area per fragment by sampling exclusion
       raster (count non-excluded pixels within each polygon)
    8. Compute available-area-weighted centroids per fragment
    9. Write output GeoPackage with all attributes

Input:
    - data/land/marine/raw/Fixed_Wind_KRA_(...).geojson — 13 fixed KRA polygons
    - data/land/marine/raw/Floating_Wind_KRA_(...).geojson — 6 floating KRA polygons
    - resources/land/offshore_exclusions.tif — binary exclusion raster (100m)
    - data/network/zonal/zones.geojson — zone boundary shapes

Output:
    - resources/land/offshore_available_kra_{network_model}.gpkg — per-zone
      KRA fragments with TG, cost tier, carrier, distance, available area

Author: K O'Neill
Date: 2026-03-22
"""

import logging
import re
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.mask
from shapely.ops import nearest_points

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
    load_and_reproject_vector,
    load_zone_shapes,
    validate_crs,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "build_offshore_available_kra"
)
logger = setup_logging(log_path)


# =============================================================================
# CONSTANTS — TG CLASSIFICATION AND COST TIER LOOKUPS
# =============================================================================

# Fixed offshore: TG → (cost_tier, capex_multiplier)
FIXED_COST_TIERS = {
    "TG-1": ("F1", 1.00),
    "TG-2A": ("F1", 1.00),
    "TG-2B": ("F1", 1.00),
    "TG-4A": ("F1", 1.00),
    "TG-3A": ("F2a", 1.10),
    "TG-3B": ("F2a", 1.10),
    "TG-4B": ("F2a", 1.10),
    "TG-5A": ("F2b", 1.15),
    "TG-5B": ("F2b", 1.15),
    "TG-6A": ("F3a", 1.20),
    "TG-6B": ("F3a", 1.20),
    "TG-7A": ("F3b", 1.30),
    "TG-7B": ("F3b", 1.30),
}

# Floating offshore: TG → (cost_tier, capex_multiplier)
FLOATING_COST_TIERS = {
    "TG-1": ("FL1", 1.00),
    "TG-2": ("FL2a", 1.10),
    "TG-3": ("FL2a", 1.10),
    "TG-4": ("FL2b", 1.15),
    "TG-5": ("FL3a", 1.20),
    "TG-6": ("FL3b", 1.30),
}

# Offshore zone names (excluded from coastline derivation)
OFFSHORE_ZONES = {"DOGGER_BANK", "HORNSEA", "EAST_ANGLIA"}

# AC/DC distance threshold (metres)
AC_THRESHOLD_M = 50_000


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def parse_tg_class(rating_str):
    """Parse TG classification from KRA Rating attribute.

    Parameters
    ----------
    rating_str : str
        Raw Rating value, e.g. "Technology Group 7B" or "Technology Group 2".

    Returns
    -------
    str
        Parsed TG class, e.g. "TG-7B" or "TG-2".

    Raises
    ------
    ValueError
        If rating_str does not match expected pattern.
    """
    match = re.match(r"Technology Group (\d+[A-B]?)", rating_str)
    if not match:
        raise ValueError(f"Cannot parse TG class from Rating: '{rating_str}'")
    return f"TG-{match.group(1)}"


def classify_cost_tier(tg_class, kra_type):
    """Map TG classification to cost tier and capex multiplier.

    Parameters
    ----------
    tg_class : str
        Parsed TG class, e.g. "TG-7B".
    kra_type : str
        "fixed" or "floating".

    Returns
    -------
    tuple[str, float]
        (cost_tier, capex_multiplier), e.g. ("F3b", 1.30).

    Raises
    ------
    ValueError
        If tg_class not found in lookup for the given kra_type.
    """
    lookup = FIXED_COST_TIERS if kra_type == "fixed" else FLOATING_COST_TIERS
    if tg_class not in lookup:
        raise ValueError(
            f"Unknown TG class '{tg_class}' for {kra_type} offshore. "
            f"Valid: {sorted(lookup.keys())}"
        )
    return lookup[tg_class]


def derive_coastline(zones):
    """Derive coastline geometry from onshore zone boundaries.

    Takes the union of all onshore zone polygons and extracts the
    exterior boundary. Offshore zones (DOGGER_BANK, HORNSEA,
    EAST_ANGLIA) are excluded.

    Parameters
    ----------
    zones : gpd.GeoDataFrame
        Zone shapes with 'zone_name' column, in EPSG:27700.

    Returns
    -------
    shapely.geometry.base.BaseGeometry
        Coastline as LineString or MultiLineString in EPSG:27700.
    """
    onshore = zones[~zones["zone_name"].isin(OFFSHORE_ZONES)]
    logger.info(
        f"  Deriving coastline from {len(onshore)} onshore zones "
        f"(excluded {len(zones) - len(onshore)} offshore zones)"
    )
    coastline = onshore.geometry.union_all().boundary
    length_km = coastline.length / 1000
    logger.info(f"  Coastline length: {length_km:.0f} km")
    return coastline


def classify_carrier(kra_type, distance_m):
    """Classify carrier and connection type based on KRA type and distance.

    Parameters
    ----------
    kra_type : str
        "fixed" or "floating".
    distance_m : float
        Distance from KRA centroid to nearest coastline point (metres).

    Returns
    -------
    tuple[str, str]
        (carrier, connection_type), e.g. ("offwind-fixed-ac", "AC").
    """
    if distance_m < AC_THRESHOLD_M:
        connection_type = "AC"
    else:
        connection_type = "DC"

    # Use "float" not "floating" in carrier name for consistency
    type_label = "float" if kra_type == "floating" else kra_type
    carrier = f"offwind-{type_label}-{connection_type.lower()}"
    return carrier, connection_type


def calculate_fragment_availability(fragment_geom, exclusion_src, resolution_m=100):
    """Calculate available area and centroid for one KRA-zone fragment.

    Samples the offshore exclusion raster within the fragment polygon,
    counts non-excluded pixels, and computes an area-weighted centroid
    from the available pixel locations.

    Parameters
    ----------
    fragment_geom : shapely.geometry.base.BaseGeometry
        Polygon geometry of the KRA-zone intersection fragment.
    exclusion_src : rasterio.DatasetReader
        Open rasterio dataset for offshore_exclusions.tif.
    resolution_m : int or float
        Raster resolution in metres (default 100).

    Returns
    -------
    tuple[float, float, float]
        (available_area_km2, centroid_x, centroid_y).
        If no available pixels, returns (0.0, polygon_centroid.x, polygon_centroid.y).
    """
    try:
        clipped, clipped_transform = rasterio.mask.mask(
            exclusion_src,
            [fragment_geom],
            crop=True,
            nodata=255,
            all_touched=True,
        )
    except ValueError:
        # Fragment doesn't overlap raster extent
        centroid = fragment_geom.centroid
        return 0.0, centroid.x, centroid.y

    # clipped shape is (1, rows, cols) — extract single band
    data = clipped[0]

    # Available pixels: value == 0 (not excluded) and not nodata
    available_mask = data == 0
    n_available = int(available_mask.sum())

    if n_available == 0:
        centroid = fragment_geom.centroid
        return 0.0, centroid.x, centroid.y

    # Calculate area
    pixel_area_km2 = (resolution_m * resolution_m) / 1e6
    available_area_km2 = n_available * pixel_area_km2

    # Calculate centroid from available pixel coordinates
    rows, cols = np.where(available_mask)
    # Transform pixel indices to EPSG:27700 coordinates
    xs = clipped_transform.c + (cols + 0.5) * clipped_transform.a
    ys = clipped_transform.f + (rows + 0.5) * clipped_transform.e
    centroid_x = float(xs.mean())
    centroid_y = float(ys.mean())

    return available_area_km2, centroid_x, centroid_y


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD OFFSHORE AVAILABLE KRA")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "fixed_kra": snk.input.fixed_kra,
            "floating_kra": snk.input.floating_kra,
            "exclusion_raster": snk.input.exclusion_raster,
            "zones": snk.input.zones,
        }
        output_path = snk.output.available_kra
        land_config = snk.params.config
    else:
        input_paths = {
            "fixed_kra": "data/land/marine/raw/Fixed_Wind_KRA_(England26_NI)%2C_The_Crown_Estate.geojson",
            "floating_kra": "data/land/marine/raw/Floating_Wind_KRA_(England26_NI)%2C_The_Crown_Estate.geojson",
            "exclusion_raster": "resources/land/offshore_exclusions.tif",
            "zones": "data/network/zonal/zones.geojson",
        }
        output_path = "resources/land/offshore_available_kra_Zonal.gpkg"
        land_config = {}

    foundation = land_config.get("foundation", {})
    target_crs = f"EPSG:{foundation.get('target_crs', 27700)}"

    # Stage 1: Validate inputs
    stage_start = time.time()
    for name, path in input_paths.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"Input not found: {name} → {path}")
        logger.info(f"  Input {name}: {path}")
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Load and classify KRAs
    stage_start = time.time()
    logger.info("Loading and classifying KRA polygons...")

    fixed_kra = load_and_reproject_vector(input_paths["fixed_kra"], target_crs=target_crs)
    fixed_kra["kra_type"] = "fixed"
    logger.info(f"  Fixed KRA: {len(fixed_kra)} polygons")

    floating_kra = load_and_reproject_vector(input_paths["floating_kra"], target_crs=target_crs)
    floating_kra["kra_type"] = "floating"
    logger.info(f"  Floating KRA: {len(floating_kra)} polygons")

    # Concatenate and classify
    kras = pd.concat([fixed_kra, floating_kra], ignore_index=True)
    kras = gpd.GeoDataFrame(kras, geometry="geometry", crs=fixed_kra.crs)

    # Parse TG classification
    kras["kra_name"] = kras["Rating"]
    kras["tg_class"] = kras["Rating"].apply(parse_tg_class)

    # Map to cost tier and multiplier
    tier_data = kras.apply(
        lambda row: classify_cost_tier(row["tg_class"], row["kra_type"]),
        axis=1,
    )
    kras["cost_tier"] = tier_data.apply(lambda x: x[0])
    kras["capex_multiplier"] = tier_data.apply(lambda x: x[1])

    logger.info(f"  Total KRAs: {len(kras)}")
    logger.info(f"  TG classes: {sorted(kras['tg_class'].unique())}")
    stage_times["Load and classify KRAs"] = time.time() - stage_start

    # Stage 3: Derive coastline and calculate distances
    stage_start = time.time()
    logger.info("Deriving coastline and calculating distances...")

    zones = load_zone_shapes(input_paths["zones"], target_crs=target_crs)
    coastline = derive_coastline(zones)

    # Calculate distance from each KRA centroid to nearest coastline
    distances_m = []
    for _, row in kras.iterrows():
        centroid = row.geometry.centroid
        nearest_pt = nearest_points(centroid, coastline)[1]
        dist_m = centroid.distance(nearest_pt)
        distances_m.append(dist_m)

    kras["distance_to_coast_km"] = [d / 1000 for d in distances_m]

    # Classify carrier and connection type
    carrier_data = kras.apply(
        lambda row: classify_carrier(row["kra_type"], row["distance_to_coast_km"] * 1000),
        axis=1,
    )
    kras["carrier"] = carrier_data.apply(lambda x: x[0])
    kras["connection_type"] = carrier_data.apply(lambda x: x[1])

    for _, row in kras.iterrows():
        logger.info(
            f"  {row['tg_class']} ({row['kra_type']}): "
            f"{row['distance_to_coast_km']:.1f} km → {row['carrier']}"
        )
    stage_times["Coastline and distances"] = time.time() - stage_start

    # Stage 4: Intersect KRAs with zone boundaries
    stage_start = time.time()
    logger.info("Intersecting KRAs with zone boundaries...")

    # Select columns to preserve through overlay
    kra_cols = [
        "kra_name", "tg_class", "cost_tier", "capex_multiplier",
        "kra_type", "carrier", "connection_type", "distance_to_coast_km",
        "geometry",
    ]
    kras_for_overlay = kras[kra_cols].copy()

    fragments = gpd.overlay(kras_for_overlay, zones, how="intersection")
    logger.info(f"  KRA-zone fragments: {len(fragments)}")
    stage_times["Zone intersection"] = time.time() - stage_start

    # Stage 5: Calculate available area per fragment
    stage_start = time.time()
    logger.info("Calculating available area per fragment...")

    available_areas = []
    centroid_xs = []
    centroid_ys = []

    with rasterio.open(input_paths["exclusion_raster"]) as exclusion_src:
        for idx, row in fragments.iterrows():
            area_km2, cx, cy = calculate_fragment_availability(
                row.geometry, exclusion_src
            )
            available_areas.append(area_km2)
            centroid_xs.append(cx)
            centroid_ys.append(cy)

    fragments["available_area_km2"] = available_areas
    fragments["centroid_x"] = centroid_xs
    fragments["centroid_y"] = centroid_ys

    # Drop fragments with no available area
    n_before = len(fragments)
    fragments = fragments[fragments["available_area_km2"] > 0].copy()
    n_dropped = n_before - len(fragments)
    if n_dropped > 0:
        logger.warning(f"  Dropped {n_dropped} fragments with zero available area")
    logger.info(f"  Remaining fragments: {len(fragments)}")

    total_area = fragments["available_area_km2"].sum()
    logger.info(f"  Total available area: {total_area:.1f} km²")
    stage_times["Calculate available area"] = time.time() - stage_start

    # Stage 6: Write output
    stage_start = time.time()
    logger.info("Writing output GeoPackage...")

    # Select and order output columns
    output_cols = [
        "kra_name", "tg_class", "cost_tier", "capex_multiplier",
        "kra_type", "carrier", "connection_type", "distance_to_coast_km",
        "zone_name", "available_area_km2", "centroid_x", "centroid_y",
        "geometry",
    ]
    output_gdf = fragments[output_cols].copy()

    # Ensure output directory exists
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    output_gdf.to_file(output_path, driver="GPKG")
    logger.info(f"  Written: {output_path} ({len(output_gdf)} features)")

    # Log breakdown by carrier
    for carrier, group in output_gdf.groupby("carrier"):
        logger.info(
            f"  {carrier}: {len(group)} fragments, "
            f"{group['available_area_km2'].sum():.1f} km²"
        )

    stage_times["Write output"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Offshore Available KRA",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "total_kras": len(kras),
            "total_fragments": len(output_gdf),
            "total_available_km2": f"{total_area:.1f}",
            "carriers": sorted(output_gdf["carrier"].unique().tolist()),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD OFFSHORE AVAILABLE KRA — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
