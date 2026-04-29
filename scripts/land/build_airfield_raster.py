#!/usr/bin/env python3
"""
Build 2-band FRZ exclusion raster from aerodrome Flight Restriction Zone data.

Reads civil and military aerodrome FRZ CSVs (extracted from UK AIP ENR 5.1)
with centre coordinates and FRZ circle radii. Buffers each point by its FRZ
radius and rasterizes to a 2-band GeoTIFF.

The FRZ radius IS the exclusion zone — no downstream buffering is needed.
All technologies (onwind, solar, nuclear SMR) apply this raster as a hard
exclusion mask.

Processing steps:
1. Load civil and MoD FRZ CSVs with lat/lon and radius
2. Create Point geometries in WGS84, reproject to EPSG:27700
3. Buffer each point by FRZ radius (km → metres) to create exclusion polygons
4. Rasterize MoD circles to Band 1, civilian circles to Band 2
5. Write 2-band GeoTIFF

Input:
    - data/land/hazards/civil_aerodromes_frz.csv
    - data/land/hazards/mod_aerodromes_frz.csv

Output:
    - resources/land/airfields_gb.tif — 2-band GeoTIFF (100m, EPSG:27700)
        Band 1: MoD/Military FRZ circles
        Band 2: Civilian FRZ circles

Author: K O'Neill
Date: 2026-03-25
"""

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

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
    rasterize_vector,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_airfield_raster"
logger = setup_logging(log_path)


# =============================================================================
# CONSTANTS
# =============================================================================

# Polygon raster bands — 2-band output
POLYGON_BAND_NAMES = ["MoD_Military", "Civilian"]

# Columns to read from FRZ CSVs (ignore trailing Excel columns)
FRZ_USECOLS = [
    "frz_id", "aerodrome_name", "operator_type",
    "lat_dd", "lon_dd", "frz_radius_km",
]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def validate_inputs(input_paths):
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
            f"Missing {len(missing)} input file(s):\n" + "\n".join(missing)
        )

    logger.info(f"All {len(input_paths)} input files validated.")


def load_frz_data(csv_path, target_crs="EPSG:27700"):
    """
    Load FRZ CSV and create a GeoDataFrame with Point geometries.

    Reads the CSV with lat/lon coordinates (WGS84), creates Point
    geometries, reprojects to the target CRS, and computes the
    FRZ radius in metres for buffering.

    Parameters
    ----------
    csv_path : str or Path
        Path to FRZ CSV file.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        FRZ data with Point geometries in target CRS and
        ``frz_radius_m`` column (radius in metres).

    Raises
    ------
    ValueError
        If CSV contains rows with missing coordinates or radius.
    """
    df = pd.read_csv(csv_path, encoding="latin-1", usecols=FRZ_USECOLS)
    logger.info(f"Loaded {len(df)} FRZ entries from {csv_path}")

    # Validate no missing values in critical columns
    for col in ["lat_dd", "lon_dd", "frz_radius_km"]:
        n_missing = df[col].isna().sum()
        if n_missing > 0:
            raise ValueError(
                f"FAILED: {csv_path} — {n_missing} missing values in '{col}'"
            )

    # Create Point geometries from lon/lat (WGS84)
    geometry = [Point(lon, lat) for lon, lat in zip(df["lon_dd"], df["lat_dd"])]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")

    # Reproject to target CRS (metres) for buffering
    gdf = gdf.to_crs(target_crs)

    # Compute radius in metres
    gdf["frz_radius_m"] = gdf["frz_radius_km"] * 1000.0

    logger.info(
        f"  Radii: min={gdf['frz_radius_km'].min():.1f} km, "
        f"max={gdf['frz_radius_km'].max():.1f} km"
    )

    return gdf


def buffer_frz_to_circles(gdf):
    """
    Buffer each FRZ point by its radius to create exclusion polygons.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        FRZ data with Point geometries and ``frz_radius_m`` column.

    Returns
    -------
    gpd.GeoDataFrame
        FRZ data with Polygon geometries (buffered circles).
    """
    gdf = gdf.copy()
    gdf["geometry"] = gdf.apply(
        lambda row: row.geometry.buffer(row["frz_radius_m"]), axis=1
    )
    logger.info(f"  Buffered {len(gdf)} FRZ points to exclusion circles")
    return gdf


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD AIRFIELD FRZ EXCLUSION RASTER")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        civil_csv = snk.input.civil_frz
        mod_csv = snk.input.mod_frz
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = f"EPSG:{snk.params.target_crs}"
    else:
        civil_csv = "data/land/hazards/civil_aerodromes_frz.csv"
        mod_csv = "data/land/hazards/mod_aerodromes_frz.csv"
        output_raster = "resources/land/airfields_gb.tif"
        resolution = 100
        target_crs = "EPSG:27700"

    # Validate inputs
    validate_inputs({"civil_frz": civil_csv, "mod_frz": mod_csv})

    # Stage 1: Load FRZ data
    stage_start = time.time()
    logger.info("Stage 1: Loading FRZ data from CSVs...")
    civil_gdf = load_frz_data(civil_csv, target_crs=target_crs)
    mod_gdf = load_frz_data(mod_csv, target_crs=target_crs)
    logger.info(f"  Total: {len(civil_gdf)} civil + {len(mod_gdf)} MoD = {len(civil_gdf) + len(mod_gdf)} aerodromes")
    stage_times["Load FRZ data"] = time.time() - stage_start

    # Stage 2: Buffer points to FRZ circles
    stage_start = time.time()
    logger.info("Stage 2: Buffering FRZ points to exclusion circles...")
    civil_circles = buffer_frz_to_circles(civil_gdf)
    mod_circles = buffer_frz_to_circles(mod_gdf)
    stage_times["Buffer FRZ circles"] = time.time() - stage_start

    # Stage 3: Create 2-band raster
    stage_start = time.time()
    logger.info("Stage 3: Creating 2-band FRZ exclusion raster...")

    width, height, transform, crs = create_reference_grid(
        resolution=resolution, crs=target_crs
    )
    template = (width, height, transform, crs)
    logger.info(f"Reference grid: {width}x{height} pixels, {resolution}m resolution")

    band_mod = rasterize_vector(mod_circles, template, burn_value=1, dtype="uint8")
    logger.info(f"  MoD_Military: {int(band_mod.sum())} pixels")

    band_civilian = rasterize_vector(civil_circles, template, burn_value=1, dtype="uint8")
    logger.info(f"  Civilian: {int(band_civilian.sum())} pixels")

    airfield_raster = np.stack([band_mod, band_civilian], axis=0)
    logger.info(f"Airfield raster shape: {airfield_raster.shape}")
    stage_times["Rasterize FRZ circles"] = time.time() - stage_start

    # Stage 4: Write output
    stage_start = time.time()
    logger.info("Stage 4: Writing output...")

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 2,
        "crs": target_crs,
        "transform": transform,
    }

    Path(output_raster).parent.mkdir(parents=True, exist_ok=True)
    write_geotiff(airfield_raster, profile, output_raster, band_names=POLYGON_BAND_NAMES)
    logger.info(f"  Written 2-band raster: {output_raster}")

    stage_times["Write output"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="build_airfield_raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={"resolution": resolution, "target_crs": target_crs},
    )

    logger.info("=" * 80)
    logger.info("Script Complete")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
