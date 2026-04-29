#!/usr/bin/env python3
"""
Build single-band coastal erosion exclusion raster from NCERM 2024 data.

Loads 3 layers from the National Coastal Erosion Risk Mapping (NCERM) 2024
GeoPackage and OR-merges them into a single binary exclusion raster at 100m
resolution in EPSG:27700.

Layers merged:
- NCERM_SMP_2105_70CC: Shoreline Management Plan delivered erosion extent
  at year 2105 under Higher Central climate change allowance (UKCP18 RCP8.5
  70th percentile sea level rise). The 2105 horizon reflects the long asset
  life of energy infrastructure, particularly nuclear (~60 years).
- NCERM_Ground_Instability_Zone: Areas of historical ground instability
  (cliff recession, landslides) mapped from observed geomorphological
  evidence.
- NCERM_Ground_Instability_Recession: Predicted future ground instability
  recession zones based on geological and geotechnical assessment.

Output is binary (1 = erosion/instability risk, 0 = safe). Overlapping
polygons across layers are handled idempotently via OR-merge (np.maximum).

Technology branches apply their own interpretation:
- Onshore wind/solar: hard exclusion
- Nuclear SMR: exclusion with configurable buffer (EN-7 requirement)
- Hydrogen electrolysis/turbine: hard exclusion

Processing stages:
    1. Validate input GPKG exists
    2. Create canonical GB reference grid (shared across all foundation rasters)
    3. For each layer: load via geopandas, rasterize, OR-merge into accumulator
    4. Write output: single-band GeoTIFF

Input:
    - data/land/hazards/coastal_erosion_uk_2024.gpkg — NCERM 2024 multi-layer
      GeoPackage (14 layers). Source: Environment Agency, National Coastal
      Erosion Risk Mapping (NCERM) - National (2024).
      https://environment.data.gov.uk/dataset/9fede91f-5acd-4fd2-9bd8-98153fa3c2ff

Output:
    - resources/land/coastal_erosion_gb.tif — single-band GeoTIFF (uint8,
      EPSG:27700, 100m resolution). Binary mask: 1 = erosion/instability
      risk area, 0 = safe.

Author: K O'Neill
Date: 2026-03-25
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np

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
    rasterize_vector,
    validate_gb_coverage,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "build_coastal_erosion_raster"
)
logger = setup_logging(log_path)


# =============================================================================
# CONSTANTS
# =============================================================================

DEFAULT_LAYERS = [
    "NCERM_SMP_2105_70CC",
    "NCERM_Ground_Instability_Zone",
    "NCERM_Ground_Instability_Recession",
]

BAND_NAME = "coastal_erosion"


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
        raise FileNotFoundError(
            f"Missing {len(missing)} input file(s):\n" + "\n".join(missing)
        )

    logger.info(f"All {len(input_paths)} input files validated.")


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def build_coastal_erosion_raster(
    input_path: str,
    layers: list[str],
    resolution: int,
    target_crs: str = "EPSG:27700",
    bounds: tuple[float, float, float, float] | None = None,
) -> tuple[np.ndarray, dict]:
    """
    Load NCERM layers from multi-layer GPKG, rasterize, and OR-merge.

    For each specified layer in the GeoPackage, loads the vector data,
    rasterizes to the canonical reference grid, and OR-merges into a
    single accumulator array. The result is a binary mask where 1 indicates
    erosion or ground instability risk.

    Parameters
    ----------
    input_path : str
        Path to the NCERM 2024 multi-layer GeoPackage.
    layers : list of str
        Layer names to load and merge from the GeoPackage.
    resolution : int
        Raster resolution in metres.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".
    bounds : tuple of float or None, optional
        Spatial extent as (xmin, ymin, xmax, ymax). If None, uses the
        canonical GB bounding box. Pass explicit bounds only for unit tests.

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(erosion_raster, profile)`` where *erosion_raster* has shape
        ``(height, width)`` with uint8 values (1 = risk, 0 = safe),
        and *profile* is a rasterio profile dict.
    """
    # Create reference grid
    width, height, transform, crs = create_reference_grid(
        bounds=bounds,
        resolution=resolution,
        crs=target_crs,
    )
    template = (width, height, transform, crs)
    logger.info(f"Reference grid: {width}x{height} pixels, {resolution}m resolution")

    # Initialise accumulator (all zeros)
    erosion_raster = np.zeros((height, width), dtype="uint8")

    # Track cumulative bounds for coverage validation
    cumulative_bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    total_features = 0

    # Process each layer
    for layer_name in layers:
        logger.info(f"Processing layer: {layer_name}")

        gdf = load_and_reproject_vector(input_path, target_crs=target_crs, layer=layer_name)
        n_features = len(gdf)
        total_features += n_features
        logger.info(f"  Loaded {n_features} features")

        if n_features == 0:
            logger.warning(f"  Layer {layer_name} is empty — skipping")
            continue

        # Rasterize and OR-merge
        burned = rasterize_vector(gdf, template, burn_value=1, dtype="uint8")
        np.maximum(erosion_raster, burned, out=erosion_raster)
        logger.info(f"  Rasterized: {int(burned.sum())} pixels")

        # Update cumulative bounds
        layer_bounds = gdf.total_bounds  # (xmin, ymin, xmax, ymax)
        cumulative_bounds[0] = min(cumulative_bounds[0], layer_bounds[0])
        cumulative_bounds[1] = min(cumulative_bounds[1], layer_bounds[1])
        cumulative_bounds[2] = max(cumulative_bounds[2], layer_bounds[2])
        cumulative_bounds[3] = max(cumulative_bounds[3], layer_bounds[3])

    # Validate cumulative coverage (coastal data won't cover all of GB inland)
    logger.info(f"Total features processed: {total_features} across {len(layers)} layers")
    if total_features > 0:
        validate_gb_coverage(tuple(cumulative_bounds), min_fraction=0.0)

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
    erosion_pixels = np.count_nonzero(erosion_raster)
    total_pixels = erosion_raster.size
    erosion_pct = 100.0 * erosion_pixels / total_pixels if total_pixels > 0 else 0.0
    logger.info(
        f"Coastal erosion raster: {erosion_raster.shape}, "
        f"erosion pixels: {erosion_pixels} of {total_pixels} ({erosion_pct:.2f}%)"
    )

    return erosion_raster, profile


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD COASTAL EROSION EXCLUSION RASTER")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_path = snk.input.coastal_erosion
        output_raster = snk.output.raster
        layers = snk.params.layers
        resolution = snk.params.resolution
        target_crs = snk.params.target_crs
    else:
        input_path = "data/land/hazards/coastal_erosion_uk_2024.gpkg"
        output_raster = "resources/land/coastal_erosion_gb.tif"
        layers = DEFAULT_LAYERS
        resolution = 100
        target_crs = 27700

    crs_str = f"EPSG:{target_crs}"
    logger.info(f"Input: {input_path}")
    logger.info(f"Layers: {layers}")
    logger.info(f"Resolution: {resolution}m")
    logger.info(f"Target CRS: {crs_str}")

    # Stage 1: Validate inputs
    stage_start = time.time()
    validate_inputs({"coastal_erosion": input_path})
    stage_times["Validate inputs"] = time.time() - stage_start

    # Stage 2: Build raster (load layers, rasterize, OR-merge)
    stage_start = time.time()
    erosion_raster, profile = build_coastal_erosion_raster(
        input_path=input_path,
        layers=layers,
        resolution=resolution,
        target_crs=crs_str,
    )
    stage_times["Build raster"] = time.time() - stage_start

    # Stage 3: Write GeoTIFF
    stage_start = time.time()
    Path(output_raster).parent.mkdir(parents=True, exist_ok=True)
    write_geotiff(erosion_raster, profile, output_raster, band_names=[BAND_NAME])
    logger.info(f"  Written single-band raster: {output_raster}")
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Coastal Erosion Raster",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "resolution": resolution,
            "target_crs": target_crs,
            "layers": layers,
            "raster_shape": str(erosion_raster.shape),
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD COASTAL EROSION EXCLUSION RASTER — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
