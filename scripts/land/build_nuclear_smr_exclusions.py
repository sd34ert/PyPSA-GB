#!/usr/bin/env python3
"""
Build pixel-level SMR exclusion raster and per-zone availability fractions.

Combines shared foundation exclusion layers (protected areas, airfields,
land cover, flooding, groundwater SPZ, coastal erosion, ALC BMV, Green Belt)
with nuclear-specific layers (Scotland ban, ONR population criterion,
COMAH Upper Tier buffer, high-pressure gas pipeline buffer) into a single
binary exclusion raster. Then calculates per-zone availability fractions
(fraction of zone area NOT excluded).

Follows the same pixel-level exclusion pattern as
build_onshore_renewable_exclusions.py. Helper functions for the 8
shared foundation layers are copied from that script (pure functions,
no shared state). Nuclear-specific layers are added as Steps 9-13.

Input:
    Foundation rasters (100m, EPSG:27700):
    - resources/land/protected_areas_gb.tif — 4-band protected areas
    - resources/land/airfields_gb.tif — 2-band FRZ exclusion zones
    - resources/land/land_cover_gb.tif — UK LCM 2024 (21 classes)
    - resources/land/flooding_risk_gb.tif — merged flood zones
    - resources/land/groundwater_spz_ew.tif — 3-band SPZ
    - resources/land/coastal_erosion_gb.tif — coastal change exclusion
    - resources/land/alc_bmv_gb.tif — BMV agricultural land
    - resources/land/green_belt_gb.tif — Green Belt

    Nuclear-specific:
    - resources/land/scotland_mask_{network_model}.tif — binary Scotland mask
    - resources/land/nuclear_pop_criterion_{network_model}.tif — ONR criterion
    - data/land/hazards/comah_upper-tier_ew_2025.csv — COMAH Upper Tier sites
    - data/land/hazards/Gas_Pipe.shp — High-pressure gas pipelines

    Zone shapes (for per-zone fraction calculation)

Output:
    - resources/land/smr_exclusions_{network_model}.tif — binary exclusion
      raster (0=available, 1=excluded)
    - resources/land/smr_availability_{network_model}.csv — per-zone
      availability fractions (0.0-1.0)

Author: K O'Neill
Date: 2026-03-29
"""

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from scipy.ndimage import binary_dilation

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
    buffer_geometries,
    calculate_zone_fraction,
    dissolve_overlaps,
    load_and_reproject_vector,
    load_zone_shapes,
    rasterize_vector,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "build_nuclear_smr_exclusions"
)
logger = setup_logging(log_path)


# =============================================================================
# CONSTANTS
# =============================================================================

TARGET_CRS = "EPSG:27700"
DEFAULT_COMAH_BUFFER = 3000   # metres
DEFAULT_GAS_PIPE_BUFFER = 100  # metres


# =============================================================================
# SHARED FOUNDATION HELPERS (from build_onshore_renewable_exclusions.py)
# =============================================================================


def _disk(radius):
    """Create a disk-shaped structuring element for morphological dilation.

    Parameters
    ----------
    radius : int
        Radius of the disk in pixels.

    Returns
    -------
    np.ndarray
        Binary 2-D array with 1s forming a filled circle.
    """
    L = np.arange(-radius, radius + 1)
    X, Y = np.meshgrid(L, L)
    return (X**2 + Y**2 <= radius**2).astype(np.uint8)


def buffer_distance_to_pixels(distance_m, resolution_m):
    """Convert a buffer distance in metres to pixel radius.

    Parameters
    ----------
    distance_m : int or float
        Buffer distance in metres.
    resolution_m : int or float
        Raster resolution in metres per pixel.

    Returns
    -------
    int
        Number of pixels for the disk radius. Returns 0 if distance_m <= 0.
    """
    if distance_m <= 0:
        return 0
    return max(1, int(round(distance_m / resolution_m)))


def apply_raster_buffer(mask, distance_m, resolution_m):
    """Apply spatial buffer to a binary mask using morphological dilation.

    Uses iterative dilation with a small 3x3 disk structuring element
    to keep memory constant regardless of buffer distance.

    Parameters
    ----------
    mask : np.ndarray
        2-D binary array (1 = feature, 0 = background).
    distance_m : int or float
        Buffer distance in metres. If <= 0, returns the mask unchanged.
    resolution_m : int or float
        Raster resolution in metres per pixel.

    Returns
    -------
    np.ndarray
        Buffered binary mask (uint8).
    """
    radius_px = buffer_distance_to_pixels(distance_m, resolution_m)
    if radius_px == 0:
        return mask.astype("uint8")
    structure = _disk(1)  # 3x3 disk, constant memory
    buffered = binary_dilation(
        mask.astype(bool), structure=structure, iterations=radius_px
    )
    return buffered.astype("uint8")


def apply_protected_area_exclusions(exclusion, protected_path, tech_config):
    """Apply protected area exclusions per tier (hard exclusion).

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place (logical OR).
    protected_path : str or Path
        Path to protected_areas_gb.tif (4-band).
    tech_config : dict
        Technology config dict containing 'protected_tiers' key.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    protected_tiers = tech_config.get("protected_tiers", {})

    with rasterio.open(protected_path) as src:
        for tier_num in range(1, 5):
            tier_key = f"tier{tier_num}"
            enabled = protected_tiers.get(tier_key, True)

            if not enabled:
                logger.info(f"  Tier {tier_num}: disabled — skipped")
                continue

            tier_mask = src.read(tier_num).astype(bool)
            exclusion |= tier_mask
            logger.info(
                f"  Tier {tier_num}: hard exclusion "
                f"({int(tier_mask.sum())} pixels)"
            )

    return exclusion


def apply_airfield_exclusions(exclusion, airfields_path, tech_config):
    """Apply FRZ exclusion zones from pre-buffered airfield raster.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    airfields_path : str or Path
        Path to airfields_gb.tif (2-band FRZ exclusion raster).
    tech_config : dict
        Technology config with optional 'airfield_frz' key containing
        'enabled' boolean (default True).

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    airfield_config = tech_config.get("airfield_frz", {})
    enabled = airfield_config.get("enabled", True)

    if not enabled:
        logger.info("  FRZ airfields: disabled — skipped")
        return exclusion

    with rasterio.open(airfields_path) as src:
        for band_idx in range(1, src.count + 1):
            mask = src.read(band_idx).astype(bool)
            exclusion |= mask
            logger.info(
                f"  FRZ band {band_idx}: {int(mask.sum())} pixels excluded"
            )

    return exclusion


def apply_land_cover_exclusions(
    exclusion, land_cover_path, tech_config, resolution_m
):
    """Apply land cover exclusions with optional per-code buffers.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    land_cover_path : str or Path
        Path to land_cover_gb.tif (single-band, uint8, codes 1-21).
    tech_config : dict
        Technology config with 'land_cover.exclusion_codes' and optional
        'land_cover.buffer_distances' keys.
    resolution_m : int or float
        Raster resolution in metres.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    lc_config = tech_config.get("land_cover", {})
    exclusion_codes = lc_config.get("exclusion_codes", [])
    buffer_distances = lc_config.get("buffer_distances", {})

    if not exclusion_codes:
        logger.info("  Land cover: no exclusion codes configured — skipped")
        return exclusion

    with rasterio.open(land_cover_path) as src:
        land_cover = src.read(1)

    for code in exclusion_codes:
        code_mask = (land_cover == code).astype(bool)
        buffer_m = buffer_distances.get(code, buffer_distances.get(str(code), 0))

        if buffer_m > 0:
            buffered = apply_raster_buffer(code_mask, buffer_m, resolution_m)
            exclusion |= buffered.astype(bool)
            logger.info(
                f"  Land cover code {code}: {buffer_m}m buffer "
                f"({int(code_mask.sum())} source pixels)"
            )
        else:
            exclusion |= code_mask
            logger.info(
                f"  Land cover code {code}: hard exclusion "
                f"({int(code_mask.sum())} pixels)"
            )

    return exclusion


def apply_flooding_exclusion(exclusion, flooding_path, tech_config):
    """Apply flooding risk as hard exclusion (no buffer).

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    flooding_path : str or Path
        Path to flooding_risk_gb.tif (single-band binary).
    tech_config : dict
        Technology config with optional 'flood_zones_enabled' key
        (default True).

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    enabled = tech_config.get("flood_zones_enabled", True)
    if not enabled:
        logger.info("  Flooding: disabled — skipped")
        return exclusion

    with rasterio.open(flooding_path) as src:
        flood_mask = src.read(1).astype(bool)
    exclusion |= flood_mask
    logger.info(f"  Flooding: hard exclusion ({int(flood_mask.sum())} pixels)")
    return exclusion


def apply_groundwater_exclusion(exclusion, groundwater_path, tech_config):
    """Apply groundwater SPZ exclusions per config.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    groundwater_path : str or Path
        Path to groundwater_spz_ew.tif (3-band).
    tech_config : dict
        Technology config with 'groundwater_protection.exclusion_zones'.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    gw_config = tech_config.get("groundwater_protection", {})
    exclusion_zones = gw_config.get("exclusion_zones", None)

    if exclusion_zones is None:
        logger.info("  Groundwater: no exclusion zones configured — skipped")
        return exclusion

    # Normalise to list (config may specify single int or list)
    if isinstance(exclusion_zones, int):
        exclusion_zones = [exclusion_zones]

    with rasterio.open(groundwater_path) as src:
        for zone in exclusion_zones:
            # SPZ1 → Band 1; SPZ 2,3 → Band 2
            band = 1 if zone == 1 else 2
            spz_mask = src.read(band).astype(bool)
            exclusion |= spz_mask
            logger.info(
                f"  Groundwater SPZ{zone} (band {band}): "
                f"{int(spz_mask.sum())} pixels excluded"
            )

    return exclusion


def apply_coastal_change_exclusion(
    exclusion, coastal_path, tech_config, resolution_m
):
    """Apply coastal change exclusion with optional buffer for nuclear.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    coastal_path : str or Path
        Path to coastal_erosion_gb.tif (single-band binary).
    tech_config : dict
        Technology config with optional 'coastal_change_buffer' key
        (metres, default 0 = no buffer).
    resolution_m : int or float
        Raster resolution in metres.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    buffer_m = tech_config.get("coastal_change_buffer", 0)

    with rasterio.open(coastal_path) as src:
        mask = src.read(1).astype(bool)

    if not mask.any():
        logger.info("  Coastal change: no pixels in raster — skipped")
        return exclusion

    if buffer_m > 0:
        buffered = apply_raster_buffer(mask, buffer_m, resolution_m)
        exclusion |= buffered.astype(bool)
        logger.info(
            f"  Coastal change: {buffer_m}m buffer "
            f"({int(mask.sum())} source pixels)"
        )
    else:
        exclusion |= mask
        logger.info(
            f"  Coastal change: hard exclusion ({int(mask.sum())} pixels)"
        )

    return exclusion


def apply_alc_bmv_exclusion(exclusion, alc_bmv_path, tech_config):
    """Apply BMV agricultural land exclusion if enabled in config.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    alc_bmv_path : str or Path
        Path to alc_bmv_gb.tif (single-band binary).
    tech_config : dict
        Technology config with optional 'alc_bmv.enabled' key
        (default False).

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    enabled = tech_config.get("alc_bmv", {}).get("enabled", False)
    if not enabled:
        logger.info("  ALC BMV: disabled — skipped")
        return exclusion

    with rasterio.open(alc_bmv_path) as src:
        mask = src.read(1).astype(bool)
    exclusion |= mask
    logger.info(f"  ALC BMV: hard exclusion ({int(mask.sum())} pixels)")
    return exclusion


def apply_green_belt_exclusion(exclusion, green_belt_path, tech_config):
    """Apply Green Belt exclusion if enabled in config.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    green_belt_path : str or Path
        Path to green_belt_gb.tif (single-band binary).
    tech_config : dict
        Technology config with optional 'green_belt.enabled' key
        (default False).

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    enabled = tech_config.get("green_belt", {}).get("enabled", False)
    if not enabled:
        logger.info("  Green Belt: disabled — skipped")
        return exclusion

    with rasterio.open(green_belt_path) as src:
        mask = src.read(1).astype(bool)
    exclusion |= mask
    logger.info(f"  Green Belt: hard exclusion ({int(mask.sum())} pixels)")
    return exclusion


# =============================================================================
# NUCLEAR-SPECIFIC HELPERS
# =============================================================================


def apply_scotland_exclusion(exclusion, scotland_mask_path, scotland_ban):
    """Apply Scotland ban as hard exclusion.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    scotland_mask_path : str or Path
        Path to scotland_mask_{network_model}.tif (single-band binary,
        1=Scotland, 0=not Scotland).
    scotland_ban : bool
        If True, all Scotland pixels are excluded. If False, skipped.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    if not scotland_ban:
        logger.info("  Scotland ban: disabled — skipped")
        return exclusion

    with rasterio.open(scotland_mask_path) as src:
        scotland = src.read(1).astype(bool)
    exclusion |= scotland
    logger.info(
        f"  Scotland ban: hard exclusion ({int(scotland.sum())} pixels)"
    )
    return exclusion


def apply_pop_criterion_exclusion(exclusion, pop_criterion_path, tech_config):
    """Apply ONR semi-urban demographic criterion exclusion.

    The population criterion raster has value 0 for eligible pixels
    (SPF_MAX < 1) and value 1 for ineligible pixels.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    pop_criterion_path : str or Path
        Path to nuclear_pop_criterion_{network_model}.tif (single-band,
        uint8: 0=eligible, 1=ineligible, 255=nodata).
    tech_config : dict
        Technology config with 'pop_density_criterion.enabled' key.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    enabled = tech_config.get("pop_density_criterion", {}).get("enabled", True)
    if not enabled:
        logger.info("  Population criterion: disabled — skipped")
        return exclusion

    with rasterio.open(pop_criterion_path) as src:
        pop = src.read(1)
        nodata = src.nodata

    # Ineligible pixels have value 1; nodata (255) also excluded
    ineligible = (pop == 1)
    if nodata is not None:
        ineligible |= (pop == nodata)

    exclusion |= ineligible
    logger.info(
        f"  Population criterion: {int(ineligible.sum())} pixels excluded "
        f"(ineligible + nodata)"
    )
    return exclusion


def apply_comah_exclusion(
    exclusion, comah_path, tech_config, template, resolution_m
):
    """Apply COMAH Upper Tier site buffer as rasterized exclusion.

    Loads COMAH CSV, creates Point geometries, buffers, and rasterizes
    to match the reference grid.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    comah_path : str or Path
        Path to COMAH Upper Tier CSV file.
    tech_config : dict
        Technology config with 'comah.enabled' and 'comah.buffer' keys.
    template : tuple
        Reference grid (width, height, transform, crs).
    resolution_m : int or float
        Raster resolution in metres.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    comah_config = tech_config.get("comah", {})
    enabled = comah_config.get("enabled", True)
    buffer_m = comah_config.get("buffer", DEFAULT_COMAH_BUFFER)

    if not enabled:
        logger.info("  COMAH: disabled — skipped")
        return exclusion

    # Load COMAH sites from CSV
    df = pd.read_csv(comah_path)
    coord_cols = ["postcode_easting", "postcode_northing"]
    df = df.dropna(subset=coord_cols)
    geometry = gpd.points_from_xy(df["postcode_easting"], df["postcode_northing"])
    comah_gdf = gpd.GeoDataFrame(df, geometry=geometry, crs=TARGET_CRS)
    logger.info(f"  COMAH: loaded {len(comah_gdf)} sites")

    # Buffer and dissolve
    comah_buffered = buffer_geometries(comah_gdf, buffer_m)
    comah_dissolved = dissolve_overlaps(comah_buffered)

    # Rasterize to match reference grid
    comah_raster = rasterize_vector(comah_dissolved, template)
    exclusion |= comah_raster.astype(bool)
    logger.info(
        f"  COMAH: {buffer_m}m buffer, "
        f"{int(comah_raster.astype(bool).sum())} pixels excluded"
    )
    return exclusion


def apply_gas_pipe_exclusion(
    exclusion, gas_pipe_path, tech_config, template, resolution_m
):
    """Apply high-pressure gas pipeline buffer as rasterized exclusion.

    Loads gas pipeline shapefile, buffers, and rasterizes to match the
    reference grid.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    gas_pipe_path : str or Path
        Path to Gas_Pipe.shp.
    tech_config : dict
        Technology config with 'gas_pipe.enabled' and 'gas_pipe.buffer' keys.
    template : tuple
        Reference grid (width, height, transform, crs).
    resolution_m : int or float
        Raster resolution in metres.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    gas_config = tech_config.get("gas_pipe", {})
    enabled = gas_config.get("enabled", True)
    buffer_m = gas_config.get("buffer", DEFAULT_GAS_PIPE_BUFFER)

    if not enabled:
        logger.info("  Gas pipe: disabled — skipped")
        return exclusion

    # Load and reproject
    gas_gdf = load_and_reproject_vector(gas_pipe_path, target_crs=TARGET_CRS)
    logger.info(f"  Gas pipe: loaded {len(gas_gdf)} features")

    # Buffer and dissolve
    gas_buffered = buffer_geometries(gas_gdf, buffer_m)
    gas_dissolved = dissolve_overlaps(gas_buffered)

    # Rasterize to match reference grid
    gas_raster = rasterize_vector(gas_dissolved, template)
    exclusion |= gas_raster.astype(bool)
    logger.info(
        f"  Gas pipe: {buffer_m}m buffer, "
        f"{int(gas_raster.astype(bool).sum())} pixels excluded"
    )
    return exclusion


def apply_water_constraint(exclusion, tech_config):
    """Apply water availability constraint (placeholder).

    When water_constraint.enabled is False (default), no pixels are
    excluded. When True, raises NotImplementedError until the water
    availability data pipeline is built.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array (not modified when disabled).
    tech_config : dict
        Technology config with 'water_constraint.enabled' key.

    Returns
    -------
    np.ndarray
        Exclusion array (unchanged when disabled).

    Raises
    ------
    NotImplementedError
        If water_constraint.enabled is True (data pipeline not yet built).
    """
    water_config = tech_config.get("water_constraint", {})
    enabled = water_config.get("enabled", False)

    if not enabled:
        logger.info("  Water availability: disabled — all zones pass")
        return exclusion

    raise NotImplementedError(
        "Water constraint is enabled in config but not yet implemented. "
        "Set nuclear.siting_constraints.smr.water_constraint.enabled: false"
    )


# =============================================================================
# MAIN PROCESSING
# =============================================================================


def build_smr_exclusion(
    input_paths, smr_config, scotland_ban, resolution_m
):
    """Build combined SMR exclusion mask from all constraint layers.

    Parameters
    ----------
    input_paths : dict
        Dict of input file paths keyed by layer name.
    smr_config : dict
        SMR technology config from nuclear.siting_constraints.smr.
    scotland_ban : bool
        If True, Scotland is excluded from SMR siting.
    resolution_m : int or float
        Raster resolution in metres.

    Returns
    -------
    np.ndarray
        2-D boolean exclusion mask (True = excluded).
    tuple
        Reference grid template (width, height, transform, crs).
    """
    # Load reference grid dimensions from protected areas raster
    with rasterio.open(input_paths["protected"]) as src:
        transform = src.transform
        width = src.width
        height = src.height
        crs = str(src.crs)
    template = (width, height, transform, crs)

    exclusion = np.zeros((height, width), dtype=bool)

    # --- Shared foundation layers (Steps 1-8) ---
    logger.info("Step 1: Protected areas")
    exclusion = apply_protected_area_exclusions(
        exclusion, input_paths["protected"], smr_config
    )

    logger.info("Step 2: Airfield FRZ")
    exclusion = apply_airfield_exclusions(
        exclusion, input_paths["airfields"], smr_config
    )

    logger.info("Step 3: Land cover exclusions")
    exclusion = apply_land_cover_exclusions(
        exclusion, input_paths["land_cover"], smr_config, resolution_m
    )

    logger.info("Step 4: Flooding risk")
    exclusion = apply_flooding_exclusion(
        exclusion, input_paths["flooding"], smr_config
    )

    logger.info("Step 5: Groundwater SPZ")
    exclusion = apply_groundwater_exclusion(
        exclusion, input_paths["groundwater"], smr_config
    )

    logger.info("Step 6: Coastal change")
    exclusion = apply_coastal_change_exclusion(
        exclusion, input_paths["coastal_erosion"], smr_config, resolution_m
    )

    logger.info("Step 7: ALC BMV agricultural land")
    exclusion = apply_alc_bmv_exclusion(
        exclusion, input_paths["alc_bmv"], smr_config
    )

    logger.info("Step 8: Green Belt")
    exclusion = apply_green_belt_exclusion(
        exclusion, input_paths["green_belt"], smr_config
    )

    # --- Nuclear-specific layers (Steps 9-13) ---
    logger.info("Step 9: Scotland ban")
    exclusion = apply_scotland_exclusion(
        exclusion, input_paths["scotland_mask"], scotland_ban
    )

    logger.info("Step 10: Population criterion (ONR demographic)")
    exclusion = apply_pop_criterion_exclusion(
        exclusion, input_paths["pop_criterion"], smr_config
    )

    logger.info("Step 11: COMAH Upper Tier buffer")
    exclusion = apply_comah_exclusion(
        exclusion, input_paths["comah"], smr_config, template, resolution_m
    )

    logger.info("Step 12: Gas pipeline buffer")
    exclusion = apply_gas_pipe_exclusion(
        exclusion, input_paths["gas_pipe"], smr_config, template, resolution_m
    )

    logger.info("Step 13: Water availability")
    exclusion = apply_water_constraint(exclusion, smr_config)

    excluded_pct = 100.0 * exclusion.sum() / exclusion.size
    logger.info(
        f"Total SMR exclusion: {excluded_pct:.1f}% of raster "
        f"({int(exclusion.sum())} / {exclusion.size} pixels)"
    )

    return exclusion, template


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD NUCLEAR SMR EXCLUSIONS")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "protected": snk.input.protected,
            "airfields": snk.input.airfields,
            "land_cover": snk.input.land_cover,
            "flooding": snk.input.flooding,
            "groundwater": snk.input.groundwater,
            "coastal_erosion": snk.input.coastal_erosion,
            "alc_bmv": snk.input.alc_bmv,
            "green_belt": snk.input.green_belt,
            "scotland_mask": snk.input.scotland_mask,
            "pop_criterion": snk.input.pop_criterion,
            "comah": snk.input.comah,
            "gas_pipe": snk.input.gas_pipe,
            "zones": snk.input.zones,
        }
        output_raster = snk.output.exclusion_raster
        output_csv = snk.output.availability_csv
        nuclear_config = snk.params.nuclear_config
        lc_config = snk.params.lc_config
        smr_config = nuclear_config.get("smr", {})
        scotland_ban = smr_config.get("scotland_ban", nuclear_config.get("scotland_ban", True))
        resolution_m = lc_config.get("foundation", {}).get("resolution", 100)
    else:
        # Standalone fallback for testing
        input_paths = {
            "protected": "resources/land/protected_areas_gb.tif",
            "airfields": "resources/land/airfields_gb.tif",
            "land_cover": "resources/land/land_cover_gb.tif",
            "flooding": "resources/land/flooding_risk_gb.tif",
            "groundwater": "resources/land/groundwater_spz_ew.tif",
            "coastal_erosion": "resources/land/coastal_erosion_gb.tif",
            "alc_bmv": "resources/land/alc_bmv_gb.tif",
            "green_belt": "resources/land/green_belt_gb.tif",
            "scotland_mask": "resources/land/scotland_mask_Zonal.tif",
            "pop_criterion": "resources/land/nuclear_pop_criterion_Zonal.tif",
            "comah": "data/land/hazards/comah_upper-tier_ew_2025.csv",
            "gas_pipe": "data/land/hazards/Gas_Pipe.shp",
            "zones": "data/network/zonal/zones.geojson",
        }
        output_raster = "resources/land/smr_exclusions_Zonal.tif"
        output_csv = "resources/land/smr_availability_unfiltered_Zonal.csv"
        smr_config = {}
        scotland_ban = True
        resolution_m = 100

    logger.info(f"Resolution: {resolution_m}m")
    logger.info(f"Scotland ban: {scotland_ban}")

    # Stage 1: Validate inputs
    stage_start = time.time()
    for name, path in input_paths.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"Input not found: {name} → {path}")
        logger.info(f"  Input {name}: {path}")
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Load zone shapes
    stage_start = time.time()
    all_zones = load_zone_shapes(input_paths["zones"])
    # Filter out offshore zones — SMR is onshore only
    offshore_zones = {"DOGGER_BANK", "HORNSEA", "EAST_ANGLIA"}
    zones = all_zones[~all_zones["zone_name"].isin(offshore_zones)]
    logger.info(
        f"Zones loaded: {len(zones)} onshore zones "
        f"(filtered {len(all_zones) - len(zones)} offshore zones)"
    )
    stage_times["Load zones"] = time.time() - stage_start

    # Stage 3: Build exclusion mask
    stage_start = time.time()
    exclusion, template = build_smr_exclusion(
        input_paths=input_paths,
        smr_config=smr_config,
        scotland_ban=scotland_ban,
        resolution_m=resolution_m,
    )
    stage_times["Build exclusion"] = time.time() - stage_start

    # Stage 4: Write exclusion raster
    stage_start = time.time()
    width, height, transform, crs = template
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
    }
    write_geotiff(
        array=exclusion.astype("uint8"),
        profile=profile,
        path=output_raster,
        band_names=["smr_exclusion"],
        nodata=255,
    )
    logger.info(f"Exclusion raster written: {output_raster}")
    stage_times["Write raster"] = time.time() - stage_start

    # Stage 5: Calculate per-zone availability and write CSV
    stage_start = time.time()
    available = (~exclusion).astype("uint8")
    zone_availability = calculate_zone_fraction(available, zones, transform)

    # Compute zone areas from geometries (CRS is EPSG:27700, units=metres)
    zone_areas = zones.set_index("zone_name")["geometry"].area / 1e6

    availability_df = pd.DataFrame({
        "zone": zone_availability.index,
        "area_km2": zone_areas.reindex(zone_availability.index).values,
        "smr_available_frac": zone_availability.values.clip(0.0, 1.0),
    })

    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    availability_df.to_csv(output_csv, index=False)
    logger.info(f"Availability CSV written: {output_csv}")
    logger.info(f"\n{availability_df.to_string(index=False)}")
    stage_times["Write CSV"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Nuclear SMR Exclusions",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution_m,
            "scotland_ban": scotland_ban,
            "zones": len(zones),
            "excluded_pct": f"{100.0 * exclusion.sum() / exclusion.size:.1f}%",
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD NUCLEAR SMR EXCLUSIONS — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
