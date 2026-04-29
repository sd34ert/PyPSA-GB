#!/usr/bin/env python3
"""
Build Population Density Surface for Great Britain.

Converts Census 2021 Output Area (OA) data from England, Wales, and Scotland
into a continuous population density raster (people/km²) at 100m resolution
in EPSG:27700.

Scotland's GPKG has population counts embedded (Popcount, HHcount, sqkm
columns; 46,363 OAs) — density is calculated as Popcount / sqkm.
England & Wales GPKG contains boundaries only (188,880 OAs with OA21CD code)
— population density (people/km²) is joined from a separate Census 2021
TS006 CSV at OA level via OA21CD → Output Areas Code.

Zone-level population statistics (mean/max density) are computed by
calculate_zone_statistics, which reads this raster as one of its inputs.
No separate zone CSV is produced here.

Processing stages:
    1. Load Scotland OA boundaries (with population counts), calculate density
    2. Load England & Wales OA boundaries, join density from Census TS006 CSV
    3. Combine into single GB-wide GeoDataFrame with density column
    4. Create canonical GB reference grid (shared across all foundation rasters)
    5. Rasterize density as continuous surface (people/km²)
    6. Write output: single-band GeoTIFF

Input:
    - data/land/societal/output_areas_ew.gpkg — E&W OA boundaries (188,880
      features, EPSG:27700). Columns: OA21CD, LSOA21CD, LSOA21NM,
      LSOA21NMW, BNG_E, BNG_N, GlobalID. No population columns.
    - data/land/societal/output_areas_ew.csv — Census 2021 TS006 population
      density at OA level (188,880 rows). Columns: Output Areas Code,
      Output Areas, Observation (density in people/km²).
      Source: ONS NOMIS TS006 filtered to OA geography.
    - data/land/societal/output_areas_scotland.gpkg — Scotland OA boundaries
      with population (46,363 features, EPSG:27700). Columns: code, HHcount
      (int), Popcount (int), sqkm (float), council, masterpc, easting,
      northing.

Output:
    - resources/land/population_density_gb.tif — single-band GeoTIFF
      (float32, EPSG:27700, 100m resolution). Continuous density values
      in people/km². Expected range: rural Scotland < 50, London > 5000.

Author: PyPSA-GB Team
Date: 2026-02-28
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
    load_and_reproject_vector,
    rasterize_continuous,
    validate_gb_coverage,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_population_density_surface"
)
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


def load_scotland_oa(
    oa_shapes_path: str,
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load Scotland OA boundaries and calculate population density.

    Scotland's GPKG has population counts embedded (Popcount, sqkm columns).
    Density is calculated as Popcount / sqkm (people/km²).

    Parameters
    ----------
    oa_shapes_path : str
        Path to Scotland Output Areas GeoPackage (46,363 features).
        Expected columns: code, Popcount (int), sqkm (float).
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        GeoDataFrame with columns: geometry, density (people/km²).
        CRS is ``target_crs``.

    Raises
    ------
    ValueError
        If required columns (Popcount, sqkm) are missing from the GPKG,
        or if any sqkm values are zero (division by zero).
    """
    logger.info(f"Loading Scotland OA boundaries from {Path(oa_shapes_path).name}")
    oa_sco = load_and_reproject_vector(oa_shapes_path, target_crs=target_crs)
    logger.info(f"Scotland OAs loaded: {len(oa_sco)} features")

    # Validate expected columns
    required_cols = {"Popcount", "sqkm"}
    missing_cols = required_cols - set(oa_sco.columns)
    if missing_cols:
        raise ValueError(
            f"Scotland GPKG missing required columns: {missing_cols}. "
            f"Available columns: {list(oa_sco.columns)}"
        )

    # Check for zero sqkm values (would cause division by zero)
    zero_area = (oa_sco["sqkm"] == 0).sum()
    if zero_area > 0:
        raise ValueError(
            f"{zero_area} Scotland OAs have sqkm=0, cannot calculate density. "
            "Check source data for invalid geometries."
        )

    # Calculate density from embedded columns
    oa_sco["density"] = oa_sco["Popcount"] / oa_sco["sqkm"]  # people/km²

    logger.info(
        f"Scotland density: mean={oa_sco['density'].mean():.1f}, "
        f"max={oa_sco['density'].max():.1f}, "
        f"min={oa_sco['density'].min():.1f} people/km²"
    )

    return oa_sco[["geometry", "density"]].copy()


def load_england_wales_oa(
    oa_shapes_path: str,
    oa_density_csv_path: str,
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load England & Wales OA boundaries and join population density from CSV.

    The GPKG contains boundaries only (no population columns). Density
    (people/km²) is joined from a separate Census 2021 TS006 CSV via
    OA21CD code.

    Parameters
    ----------
    oa_shapes_path : str
        Path to England & Wales Output Areas GeoPackage (188,880 features).
        Expected column: OA21CD.
    oa_density_csv_path : str
        Path to Census 2021 TS006 CSV with population density per OA.
        Expected columns: Output Areas Code, Observation (density people/km²).
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        GeoDataFrame with columns: geometry, density (people/km²).
        CRS is ``target_crs``.

    Raises
    ------
    ValueError
        If OA21CD column is missing from the GPKG, if the CSV join fails
        (missing OA codes), or if density values contain NaN after join.
    """
    # Load E&W boundaries
    logger.info(f"Loading E&W OA boundaries from {Path(oa_shapes_path).name}")
    oa_ew = load_and_reproject_vector(oa_shapes_path, target_crs=target_crs)
    logger.info(f"E&W OAs loaded: {len(oa_ew)} features")

    if "OA21CD" not in oa_ew.columns:
        raise ValueError(
            f"E&W GPKG missing 'OA21CD' column. Available columns: {list(oa_ew.columns)}"
        )

    # Load density CSV
    logger.info(f"Loading E&W density CSV from {Path(oa_density_csv_path).name}")
    density_csv = pd.read_csv(oa_density_csv_path)
    logger.info(f"E&W density CSV loaded: {len(density_csv)} rows")

    # Validate expected CSV columns
    required_csv_cols = {"Output Areas Code", "Observation"}
    missing_csv_cols = required_csv_cols - set(density_csv.columns)
    if missing_csv_cols:
        raise ValueError(
            f"E&W density CSV missing required columns: {missing_csv_cols}. "
            f"Available columns: {list(density_csv.columns)}"
        )

    # Rename CSV columns for join
    density_csv = density_csv.rename(
        columns={
            "Output Areas Code": "OA21CD",
            "Observation": "density",
        }
    )

    # Join density to boundaries via OA code
    oa_ew = oa_ew.merge(density_csv[["OA21CD", "density"]], on="OA21CD", how="left")

    # Validate join — all OAs should have density values
    missing_density = oa_ew["density"].isna().sum()
    if missing_density > 0:
        raise ValueError(
            f"Join failed: {missing_density} of {len(oa_ew)} E&W OAs missing density. "
            "Check that CSV OA codes match GPKG OA21CD codes."
        )

    logger.info(
        f"E&W density joined: mean={oa_ew['density'].mean():.1f}, "
        f"max={oa_ew['density'].max():.1f}, "
        f"min={oa_ew['density'].min():.1f} people/km²"
    )

    return oa_ew[["geometry", "density"]].copy()


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_population_density_surface(
    oa_sco: gpd.GeoDataFrame,
    oa_ew: gpd.GeoDataFrame,
    resolution: int,
    target_crs: str = "EPSG:27700",
) -> tuple[np.ndarray, dict]:
    """
    Combine GB OA data and rasterize into a continuous density surface.

    Merges Scotland and England & Wales OA GeoDataFrames (each with a
    ``density`` column in people/km²), creates a canonical reference grid,
    and rasterizes the density values into a single-band float32 raster.

    Parameters
    ----------
    oa_sco : gpd.GeoDataFrame
        Scotland OA boundaries with density column (from :func:`load_scotland_oa`).
    oa_ew : gpd.GeoDataFrame
        England & Wales OA boundaries with density column
        (from :func:`load_england_wales_oa`).
    resolution : int
        Raster resolution in metres.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(density_raster, profile)`` where *density_raster* has shape
        ``(height, width)`` with float32 density values (people/km²),
        and *profile* is a rasterio profile dict.
    """
    # Stage 2: Combine GB-wide
    logger.info("Combining Scotland and E&W OA data into GB-wide dataset...")
    oa_gb = gpd.GeoDataFrame(
        pd.concat([oa_ew, oa_sco], ignore_index=True),
        crs=target_crs,
    )
    logger.info(
        f"GB combined: {len(oa_gb)} OAs, "
        f"density range: {oa_gb['density'].min():.1f} – {oa_gb['density'].max():.1f} people/km²"
    )

    # Validate GB coverage
    validate_gb_coverage(oa_gb)

    # Stage 3: Create reference grid (canonical GB bounds)
    width, height, transform, crs = create_reference_grid(
        resolution=resolution,
        crs=target_crs,
    )
    template = (width, height, transform, crs)

    # Stage 4: Rasterize density as continuous surface
    logger.info("Rasterizing population density...")
    density_raster = rasterize_continuous(
        oa_gb,
        template,
        value_column="density",
    )

    # Build rasterio profile
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": target_crs,
        "transform": transform,
    }

    # Log summary statistics
    valid_pixels = density_raster[density_raster != -9999.0]
    logger.info(
        f"Population density raster: {density_raster.shape}, "
        f"valid pixels: {len(valid_pixels)} of {density_raster.size}, "
        f"mean={valid_pixels.mean():.1f}, max={valid_pixels.max():.1f} people/km²"
    )

    return density_raster, profile


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD POPULATION DENSITY SURFACE")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "oa_shapes_ew": snk.input.oa_shapes_ew,
            "oa_density_ew": snk.input.oa_density_ew,
            "oa_shapes_sco": snk.input.oa_shapes_sco,
        }
        output_raster = snk.output.raster
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        input_paths = {
            "oa_shapes_ew": "data/land/societal/output_areas_ew.gpkg",
            "oa_density_ew": "data/land/societal/output_areas_ew.csv",
            "oa_shapes_sco": "data/land/societal/output_areas_scotland.gpkg",
        }
        output_raster = "resources/land/population_density_gb.tif"
        resolution = 100
        target_crs = 27700

    crs_str = f"EPSG:{target_crs}"
    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 0: Validate inputs
    stage_start = time.time()
    validate_inputs(input_paths)
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 1a: Load Scotland OA data
    stage_start = time.time()
    oa_sco = load_scotland_oa(
        oa_shapes_path=input_paths["oa_shapes_sco"],
        target_crs=crs_str,
    )
    stage_times["Load Scotland OAs"] = time.time() - stage_start

    # Stage 1b: Load England & Wales OA data
    stage_start = time.time()
    oa_ew = load_england_wales_oa(
        oa_shapes_path=input_paths["oa_shapes_ew"],
        oa_density_csv_path=input_paths["oa_density_ew"],
        target_crs=crs_str,
    )
    stage_times["Load E&W OAs"] = time.time() - stage_start

    # Stages 2-4: Combine, grid, rasterize
    stage_start = time.time()
    density_raster, profile = build_population_density_surface(
        oa_sco=oa_sco,
        oa_ew=oa_ew,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 5: Write GeoTIFF
    stage_start = time.time()
    write_geotiff(density_raster, profile, output_raster)
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Population Density Surface",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "raster_shape": str(density_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD POPULATION DENSITY SURFACE — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
