#!/usr/bin/env python3
"""
Build pixel-level exclusion rasters and per-zone availability for onshore renewables.

Core of the per-technology exclusion pipeline. Combines foundation
rasters into a per-pixel exclusion mask for each onshore technology
(onwind, solar), then calculates fractional availability (0.0–1.0)
per zone. Outputs both a multi-band exclusion raster (for validation,
QGIS, thesis maps) and a per-zone availability CSV.

For each technology, 8 exclusion layers are applied in order (each
config-driven via defaults.yaml):

    1. Protected areas (protected_areas_gb.tif, 4-band) — per-tier
       enable/disable as hard exclusion.
    2. FRZ exclusion zones (airfields_gb.tif, 2-band) — MoD and Civilian
       bands toggled independently via airfield.mod/civil config.
    3. Land cover (land_cover_gb.tif, single-band) — exclude specific UK
       LCM 2024 habitat codes with optional per-code buffer distances.
    4. Flooding risk (flooding_risk_gb.tif) — hard exclusion, toggled
       via flooding.enabled config.
    5. Groundwater SPZ (groundwater_spz_ew.tif, 3-band) — exclude
       protection zones per config.
    6. Coastal change (coastal_erosion_gb.tif) — hard exclusion, toggled
       via coastal_change.enabled config.
    7. ALC BMV (alc_bmv_gb.tif) — BMV agricultural land, toggled via
       alc_bmv.enabled config.
    8. Green Belt (green_belt_gb.tif) — Green Belt land, toggled via
       green_belt.enabled config.

Buffering is implemented via scipy.ndimage.binary_dilation on raster
arrays using a disk structuring element.

Input:
    - resources/land/protected_areas_gb.tif — 4-band protected areas
    - resources/land/airfields_gb.tif — 2-band FRZ exclusion zones
      (Band 1 = MoD/Military, Band 2 = Civilian)
    - resources/land/land_cover_gb.tif — UK LCM 2024 (21 classes)
    - resources/land/flooding_risk_gb.tif — merged flood zones
    - resources/land/groundwater_spz_ew.tif — 3-band SPZ
    - resources/land/coastal_erosion_gb.tif — coastal change exclusion
    - resources/land/alc_bmv_gb.tif — BMV agricultural land
    - resources/land/green_belt_gb.tif — Green Belt
    - Zone shapes file

Output:
    - resources/land/onshore_renewable_exclusions_{network_model}.tif —
      multi-band exclusion raster (1 band per technology, 0=available,
      1=excluded). For validation, QGIS, thesis maps.
    - resources/land/onshore_renewable_availability_{network_model}.csv —
      per-zone fractional availability (0.0–1.0) per onshore technology

Author: K O'Neill
Date: 2026-03-22
Updated: 2026-03-29 — added exclusion raster output, renamed rule
"""

import logging
import sys
import time
from pathlib import Path

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
    calculate_zone_fraction,
    load_zone_shapes,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "build_onshore_renewable_availability"
)
logger = setup_logging(log_path)

# Onshore technologies to process
ONSHORE_TECHNOLOGIES = ["onwind", "solar"]


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


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


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

    Uses iterative dilation with a small 3×3 disk structuring element
    rather than a single large disk. This keeps memory constant
    regardless of buffer distance — critical for large buffers (e.g.
    15km = 150px radius) on the full GB raster (7000×13000 pixels).

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
    structure = _disk(1)  # 3×3 disk, constant memory
    buffered = binary_dilation(
        mask.astype(bool), structure=structure, iterations=radius_px
    )
    return buffered.astype("uint8")


def apply_protected_area_exclusions(
    exclusion, protected_path, tech_config
):
    """Apply protected area exclusions per tier (hard exclusion).

    Reads each band (tier) from the protected areas raster. Each tier
    is either enabled (hard exclusion) or disabled (skipped) based on
    the technology config.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place (logical OR).
    protected_path : str or Path
        Path to protected_areas_gb.tif (4-band).
    tech_config : dict
        Technology config dict containing 'protected_tiers' key.
        Each tier is ``true`` (exclude) or ``false`` (skip).

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
                logger.info(
                    f"  Tier {tier_num}: disabled — skipped"
                )
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

    Reads bands from the airfields raster (Band 1 = MoD FRZ circles,
    Band 2 = Civilian FRZ circles) and ORs enabled bands into the
    exclusion mask. Each band is toggled via config (airfield.mod,
    airfield.civil). No additional buffers — the FRZ radius is already
    baked into the raster by build_airfield_raster.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    airfields_path : str or Path
        Path to airfields_gb.tif (2-band FRZ exclusion raster).
    tech_config : dict
        Technology config with optional 'airfield' key containing
        'mod' (default True) and 'civil' (default True) booleans.

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    airfield_config = tech_config.get("airfield", {})
    band_settings = [
        ("MoD", 1, airfield_config.get("mod", True)),
        ("Civilian", 2, airfield_config.get("civil", True)),
    ]

    with rasterio.open(airfields_path) as src:
        for band_name, band_idx, enabled in band_settings:
            if not enabled:
                logger.info(f"  FRZ {band_name}: disabled — skipped")
                continue
            mask = src.read(band_idx).astype(bool)
            exclusion |= mask
            logger.info(
                f"  FRZ {band_name}: {int(mask.sum())} pixels excluded"
            )

    return exclusion


def apply_land_cover_exclusions(
    exclusion, land_cover_path, tech_config, resolution_m
):
    """Apply land cover exclusions with optional per-code buffers.

    Reads the single-band land cover raster (UK LCM 2024, codes 1–21)
    and creates binary masks for each excluded code. Codes listed in
    buffer_distances get spatial buffers; all others are hard exclusions.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    land_cover_path : str or Path
        Path to land_cover_gb.tif (single-band, uint8, codes 1–21).
    tech_config : dict
        Technology config with 'land_cover.exclusion_codes' and
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
        # buffer_distances keys are integers in YAML but may load as int or str
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
        Technology config with optional 'flooding.enabled' key
        (default True).

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    enabled = tech_config.get("flooding", {}).get("enabled", True)
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
        Path to groundwater_spz_ew.tif (2-band).
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


def apply_coastal_change_exclusion(exclusion, coastal_path, tech_config):
    """Apply coastal change exclusion if enabled in config.

    Parameters
    ----------
    exclusion : np.ndarray
        2-D binary exclusion array to update in-place.
    coastal_path : str or Path
        Path to coastal_erosion_gb.tif (single-band binary).
    tech_config : dict
        Technology config with optional 'coastal_change.enabled' key
        (default False).

    Returns
    -------
    np.ndarray
        Updated exclusion array.
    """
    enabled = tech_config.get("coastal_change", {}).get("enabled", False)
    if not enabled:
        logger.info("  Coastal change: disabled — skipped")
        return exclusion

    with rasterio.open(coastal_path) as src:
        mask = src.read(1).astype(bool)
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
    logger.info(
        f"  ALC BMV: hard exclusion ({int(mask.sum())} pixels)"
    )
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
    logger.info(
        f"  Green Belt: hard exclusion ({int(mask.sum())} pixels)"
    )
    return exclusion


def build_technology_exclusion(
    tech,
    tech_config,
    input_paths,
    template,
    resolution_m,
):
    """Build the combined exclusion mask for one onshore technology.

    Parameters
    ----------
    tech : str
        Technology name ('onwind' or 'solar').
    tech_config : dict
        Technology-specific config from defaults.yaml.
    input_paths : dict
        Dict of input file paths keyed by layer name.
    template : tuple
        Reference grid tuple (width, height, transform, crs).
    resolution_m : int or float
        Raster resolution in metres.

    Returns
    -------
    np.ndarray
        2-D boolean exclusion mask (True = excluded).
    """
    width, height = template[0], template[1]
    exclusion = np.zeros((height, width), dtype=bool)

    # Step 1: Protected areas (all onshore technologies)
    logger.info(f"[{tech}] Step 1: Protected areas")
    exclusion = apply_protected_area_exclusions(
        exclusion, input_paths["protected"], tech_config
    )

    # Step 2: FRZ exclusion zones
    logger.info(f"[{tech}] Step 2: FRZ exclusion zones")
    exclusion = apply_airfield_exclusions(
        exclusion, input_paths["airfields"], tech_config
    )

    # Step 3: Land cover exclusions
    logger.info(f"[{tech}] Step 3: Land cover exclusions")
    exclusion = apply_land_cover_exclusions(
        exclusion, input_paths["land_cover"], tech_config, resolution_m
    )

    # Step 4: Flooding risk
    logger.info(f"[{tech}] Step 4: Flooding risk")
    exclusion = apply_flooding_exclusion(
        exclusion, input_paths["flooding"], tech_config
    )

    # Step 5: Groundwater SPZ
    logger.info(f"[{tech}] Step 5: Groundwater SPZ")
    exclusion = apply_groundwater_exclusion(
        exclusion, input_paths["groundwater"], tech_config
    )

    # Step 6: Coastal change
    logger.info(f"[{tech}] Step 6: Coastal change")
    exclusion = apply_coastal_change_exclusion(
        exclusion, input_paths["coastal_erosion"], tech_config
    )

    # Step 7: ALC BMV agricultural land
    logger.info(f"[{tech}] Step 7: ALC BMV agricultural land")
    exclusion = apply_alc_bmv_exclusion(
        exclusion, input_paths["alc_bmv"], tech_config
    )

    # Step 8: Green Belt
    logger.info(f"[{tech}] Step 8: Green Belt")
    exclusion = apply_green_belt_exclusion(
        exclusion, input_paths["green_belt"], tech_config
    )

    excluded_pct = 100.0 * exclusion.sum() / exclusion.size
    logger.info(
        f"[{tech}] Total exclusion: {excluded_pct:.1f}% of raster "
        f"({int(exclusion.sum())} / {exclusion.size} pixels)"
    )

    return exclusion


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD ONSHORE RENEWABLE EXCLUSIONS & AVAILABILITY")
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
            "zones": snk.input.zones,
        }
        output_raster = snk.output.exclusion_raster
        output_csv = snk.output.availability_csv
        config = snk.params.config
        resolution_m = config.get("foundation", {}).get("resolution", 100)
    else:
        input_paths = {
            "protected": "resources/land/protected_areas_gb.tif",
            "airfields": "resources/land/airfields_gb.tif",
            "land_cover": "resources/land/land_cover_gb.tif",
            "flooding": "resources/land/flooding_risk_gb.tif",
            "groundwater": "resources/land/groundwater_spz_ew.tif",
            "coastal_erosion": "resources/land/coastal_erosion_gb.tif",
            "alc_bmv": "resources/land/alc_bmv_gb.tif",
            "green_belt": "resources/land/green_belt_gb.tif",
            "zones": "data/network/zonal/zones.geojson",
        }
        output_raster = "resources/land/onshore_renewable_exclusions_Zonal.tif"
        output_csv = "resources/land/onshore_renewable_availability_Zonal.csv"
        config = {}
        resolution_m = 100

    logger.info(f"Resolution: {resolution_m}m")

    # Stage 1: Validate inputs
    stage_start = time.time()
    for name, path in input_paths.items():
        if not Path(path).exists():
            raise FileNotFoundError(f"Input not found: {name} → {path}")
        logger.info(f"  Input {name}: {path}")
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Load reference grid
    stage_start = time.time()
    with rasterio.open(input_paths["protected"]) as src:
        transform = src.transform
        width = src.width
        height = src.height
        crs = str(src.crs)
    template = (width, height, transform, crs)

    all_zones = load_zone_shapes(input_paths["zones"])
    # Filter out offshore zones — onshore availability only
    offshore_zones = {"DOGGER_BANK", "HORNSEA", "EAST_ANGLIA"}
    zones = all_zones[~all_zones["zone_name"].isin(offshore_zones)]
    logger.info(f"Reference grid: {width} x {height} pixels")
    logger.info(
        f"Zones loaded: {len(zones)} onshore zones "
        f"(filtered {len(all_zones) - len(zones)} offshore zones)"
    )
    stage_times["Load reference data"] = time.time() - stage_start

    # Stage 3: Build exclusion masks and calculate availability per technology
    results = {}
    exclusion_masks = {}

    for tech in ONSHORE_TECHNOLOGIES:
        tech_start = time.time()
        tech_config = config.get(tech, {})
        logger.info("-" * 60)
        logger.info(f"Processing technology: {tech}")
        logger.info("-" * 60)

        # Build combined exclusion mask
        exclusion = build_technology_exclusion(
            tech=tech,
            tech_config=tech_config,
            input_paths=input_paths,
            template=template,
            resolution_m=resolution_m,
        )

        # Store exclusion mask for raster output
        exclusion_masks[tech] = exclusion

        # Calculate per-zone availability = fraction of non-excluded area
        available = (~exclusion).astype("uint8")
        zone_availability = calculate_zone_fraction(available, zones, transform)
        results[tech] = zone_availability

        stage_times[f"Process {tech}"] = time.time() - tech_start

    # Stage 4: Write multi-band exclusion raster
    stage_start = time.time()
    band_names = list(exclusion_masks.keys())
    raster_stack = np.stack(
        [exclusion_masks[tech].astype("uint8") for tech in band_names],
        axis=0,
    )
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": len(band_names),
        "crs": crs,
        "transform": transform,
    }
    write_geotiff(
        array=raster_stack,
        profile=profile,
        path=output_raster,
        band_names=band_names,
        nodata=255,
    )
    logger.info(f"Exclusion raster written: {output_raster} ({len(band_names)} bands: {band_names})")
    stage_times["Write raster"] = time.time() - stage_start

    # Stage 5: Combine results and write availability CSV
    stage_start = time.time()
    availability_df = pd.DataFrame(results)
    availability_df.index.name = "zone_name"

    # Ensure all values are in valid range
    availability_df = availability_df.clip(0.0, 1.0)

    # Add zone area (km²) from zone geometries (CRS is EPSG:27700, units=metres)
    zone_areas = zones.set_index("zone_name")["geometry"].area / 1e6
    availability_df.insert(0, "area_km2", zone_areas)

    availability_df.to_csv(output_csv)
    logger.info(f"Availability CSV written: {output_csv}")
    logger.info(f"Shape: {availability_df.shape}")
    logger.info(f"\n{availability_df.to_string()}")
    stage_times["Write CSV"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Onshore Renewable Exclusions",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution_m,
            "technologies": ONSHORE_TECHNOLOGIES,
            "zones": len(zones),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD ONSHORE RENEWABLE EXCLUSIONS — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
