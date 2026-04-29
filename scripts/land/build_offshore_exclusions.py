#!/usr/bin/env python3
"""
Build binary offshore exclusion raster from marine constraint layers.

Loads marine exclusion zone datasets covering 7 exclusion categories, clips each
to the GB Exclusive Economic Zone (EEZ), merges all exclusion geometries, and
rasterises to a single-band binary GeoTIFF aligned with the canonical GB grid.

Exclusion categories:
- Marine Protected Areas (MPAs) — UK-wide designations
- Shipping routes — density filtered to Q90 threshold (90th percentile)
- Oil & gas licensed fields
- CCS (Carbon Capture & Storage) licence areas
- Gas storage licence areas
- Tidal/wave plan option areas (Scotland + E&W)
- Marine mining & aggregates licence areas
- Historic environment (marine) designations

This script ONLY builds a binary exclusion raster (1 = excluded, 0 = available).
It does NOT process KRAs, calculate technical potential, or assign technologies.
KRA processing happens downstream in `build_renewable_availability_matrix`.

Processing steps:
1. Load each vector exclusion dataset and clip to GB EEZ via spatial intersection
2. Load shipping density raster, clip to GB EEZ via rasterio.mask, apply Q90
   threshold to derive shipping exclusion polygons
3. Merge all exclusion geometries into a unified GeoDataFrame
4. Reproject to EPSG:27700 (OSGB36) if needed
5. Rasterise to canonical GB grid (matching foundation rasters):
   1 = excluded, 0 = available
6. Write single-band GeoTIFF output

Input:
    - gb_eez.gpkg: GB Exclusive Economic Zone boundary
    - shipping_density_eu.geotiff: EU shipping density raster
    - offwind_protected_areas_gb.gpkg: GB offshore Marine Protected Areas
    - offshore_licensed_ccs_ew.gpkg: E&W CCS licensed sites
    - offshore_licensed_ccs_scotland.gpkg: Scottish CCS licensed sites
    - offshore_gas_storage_sites_gb.gpkg: GB gas storage licensed sites
    - offshore_o&g_zones_gb.gpkg: GB oil & gas licensed zones
    - marine_mining_sites_gb.gpkg: GB marine mining licensed sites
    - marine_aggregates_sites_gb.gpkg: GB marine aggregates licensed sites
    - historic_environment_marine.gpkg: Marine historic environment designations
    - wave_licensed_sites_scotland.gpkg: Scottish wave licensed sites
    - wave_licensed_sites_ew.gpkg: E&W wave licensed sites
    - tidal_licensed_sites_scotland.gpkg: Scottish tidal licensed sites
    - tidal_licensed_sites_ew.gpkg: E&W tidal licensed sites

Output:
    - offshore_exclusions.tif: Single-band binary GeoTIFF at 100m resolution
      in EPSG:27700. Values: 1 = excluded, 0 = available. Aligned with
      canonical GB grid (same CRS, resolution, extent as foundation rasters).

Author: K O'Neill
Date: 2026-03-19
"""

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.mask

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
    dissolve_overlaps,
    load_and_reproject_vector,
    merge_national_datasets,
    rasterize_vector,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0] if snk and hasattr(snk, "log") and snk.log else "build_offshore_exclusions"
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
        raise FileNotFoundError(
            f"Missing {len(missing)} input file(s):\n" + "\n".join(missing)
        )

    logger.info(f"All {len(input_paths)} input files validated.")


def load_eez(eez_path: str, target_crs: str = "EPSG:27700") -> gpd.GeoDataFrame:
    """
    Load GB EEZ boundary and dissolve to a single polygon.

    Parameters
    ----------
    eez_path : str
        Path to GB EEZ GeoPackage.
    target_crs : str, optional
        Target CRS, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Single-row GeoDataFrame containing the dissolved EEZ boundary.
    """
    eez = load_and_reproject_vector(eez_path, target_crs=target_crs)
    eez = eez.dissolve()
    logger.info(f"Loaded GB EEZ boundary: {len(eez)} feature(s)")
    return eez


def clip_vector_to_eez(
    gdf: gpd.GeoDataFrame,
    eez: gpd.GeoDataFrame,
    dataset_name: str,
) -> gpd.GeoDataFrame:
    """
    Clip a vector dataset to the GB EEZ boundary via spatial intersection.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Vector dataset to clip. Must share the same CRS as ``eez``.
    eez : gpd.GeoDataFrame
        Dissolved GB EEZ boundary polygon.
    dataset_name : str
        Human-readable name for logging.

    Returns
    -------
    gpd.GeoDataFrame
        Features intersecting the EEZ, with geometries clipped to EEZ bounds.
    """
    n_before = len(gdf)
    clipped = gpd.clip(gdf, eez)
    n_after = len(clipped)
    logger.info(
        f"  {dataset_name}: {n_before} -> {n_after} features after EEZ clip"
    )
    return clipped


def load_and_clip_vector_datasets(
    input_paths: dict[str, str],
    eez: gpd.GeoDataFrame,
    target_crs: str = "EPSG:27700",
) -> list[gpd.GeoDataFrame]:
    """
    Load all vector exclusion datasets, reproject, and clip to GB EEZ.

    For dataset pairs covering Scotland + E&W (CCS, wave, tidal), the
    national files are merged before clipping.

    Parameters
    ----------
    input_paths : dict of str to str
        Mapping of Snakemake input names to file paths. Expected keys:
        marine_protected_gb, ccs_ew, ccs_sco, gas_storage_gb, og_areas_gb,
        marine_mining_gb, marine_aggregates_gb, wave_sco, wave_ew,
        tidal_sco, tidal_ew.
    eez : gpd.GeoDataFrame
        Dissolved GB EEZ boundary in ``target_crs``.
    target_crs : str, optional
        Target CRS, by default "EPSG:27700".

    Returns
    -------
    list of gpd.GeoDataFrame
        Each element is a clipped GeoDataFrame for one exclusion category.
    """
    clipped_datasets = []

    # --- Single-file GB-wide datasets ---
    single_datasets = {
        "Marine Protected Areas": input_paths["marine_protected_gb"],
        "Gas storage": input_paths["gas_storage_gb"],
        "Oil & gas zones": input_paths["og_areas_gb"],
        "Marine mining": input_paths["marine_mining_gb"],
        "Marine aggregates": input_paths["marine_aggregates_gb"],
        "Historic environment (marine)": input_paths["historic_environment_marine"],
    }

    for name, path in single_datasets.items():
        gdf = load_and_reproject_vector(path, target_crs=target_crs)
        clipped = clip_vector_to_eez(gdf, eez, name)
        if not clipped.empty:
            clipped_datasets.append(clipped)

    # --- Multi-nation datasets (Scotland + E&W) ---
    nation_pairs = {
        "CCS licences": [input_paths["ccs_sco"], input_paths["ccs_ew"]],
        "Wave licences": [input_paths["wave_sco"], input_paths["wave_ew"]],
        "Tidal licences": [input_paths["tidal_sco"], input_paths["tidal_ew"]],
    }

    for name, paths in nation_pairs.items():
        merged = merge_national_datasets(paths, target_crs=target_crs)
        clipped = clip_vector_to_eez(merged, eez, name)
        if not clipped.empty:
            clipped_datasets.append(clipped)

    logger.info(
        f"Loaded {len(clipped_datasets)} vector exclusion categories "
        f"(total features: {sum(len(d) for d in clipped_datasets)})"
    )
    return clipped_datasets


def load_and_clip_shipping_density(
    shipping_path: str,
    eez: gpd.GeoDataFrame,
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load shipping density raster, clip to GB EEZ, and threshold at Q90.

    Reads the EU-wide shipping density GeoTIFF, masks it to the GB EEZ
    boundary, computes the 90th percentile of non-zero density values,
    and polygonises pixels at or above that threshold into exclusion
    geometries.

    Parameters
    ----------
    shipping_path : str
        Path to EU shipping density GeoTIFF.
    eez : gpd.GeoDataFrame
        Dissolved GB EEZ boundary. Used for rasterio mask and as the
        output CRS reference.
    target_crs : str, optional
        Target CRS for the output polygons, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Polygons of high-density shipping routes (>= Q90 threshold)
        in ``target_crs``.
    """
    from rasterio.features import shapes
    from shapely.geometry import shape

    # Reproject EEZ to the shipping raster's CRS for masking
    with rasterio.open(shipping_path) as src:
        shipping_crs = src.crs
        eez_mask = eez.to_crs(shipping_crs)
        mask_geoms = eez_mask.geometry.values

        # Clip raster to EEZ extent
        clipped_data, clipped_transform = rasterio.mask.mask(
            src, mask_geoms, crop=True, nodata=0
        )
        clipped_data = clipped_data[0]  # Single band

    logger.info(
        f"Shipping density raster clipped to EEZ: "
        f"{clipped_data.shape[0]}x{clipped_data.shape[1]} pixels"
    )

    # Compute Q90 threshold on non-zero values within EEZ
    nonzero = clipped_data[clipped_data > 0]
    if len(nonzero) == 0:
        logger.warning("No non-zero shipping density values within EEZ — skipping")
        return gpd.GeoDataFrame(geometry=[], crs=target_crs)

    q90 = np.percentile(nonzero, 90)
    logger.info(
        f"Shipping density Q90 threshold: {q90:.2f} "
        f"(from {len(nonzero)} non-zero pixels)"
    )

    # Create binary mask: 1 where density >= Q90
    high_density = (clipped_data >= q90).astype(np.uint8)
    n_excluded = int(high_density.sum())
    logger.info(f"Shipping exclusion pixels (>= Q90): {n_excluded}")

    # Polygonise high-density pixels
    polygons = []
    for geom, value in shapes(high_density, transform=clipped_transform):
        if value == 1:
            polygons.append(shape(geom))

    if not polygons:
        logger.warning("No shipping exclusion polygons generated")
        return gpd.GeoDataFrame(geometry=[], crs=target_crs)

    shipping_gdf = gpd.GeoDataFrame(geometry=polygons, crs=shipping_crs)
    shipping_gdf = shipping_gdf.to_crs(target_crs)
    logger.info(
        f"  Shipping routes (Q90): {len(shipping_gdf)} exclusion polygons"
    )

    return shipping_gdf


# =============================================================================
# MAIN PROCESSING FUNCTIONS
# =============================================================================


def merge_all_exclusions(
    vector_datasets: list[gpd.GeoDataFrame],
    shipping_exclusions: gpd.GeoDataFrame,
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Merge all exclusion geometries into a single dissolved GeoDataFrame.

    Concatenates vector exclusion categories and shipping route polygons,
    ensures CRS is ``target_crs``, then dissolves overlapping geometries
    so each pixel is counted only once during rasterisation.

    Parameters
    ----------
    vector_datasets : list of gpd.GeoDataFrame
        Clipped vector exclusion datasets (one per category).
    shipping_exclusions : gpd.GeoDataFrame
        Polygonised high-density shipping routes from Q90 threshold.
    target_crs : str, optional
        Target CRS for the merged output, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Dissolved, non-overlapping exclusion polygons in ``target_crs``.

    Raises
    ------
    ValueError
        If no exclusion geometries are available to merge.
    """
    all_gdfs = []

    for gdf in vector_datasets:
        if not gdf.empty:
            # Keep only geometry column to avoid schema conflicts on concat
            all_gdfs.append(gdf[["geometry"]])

    if not shipping_exclusions.empty:
        all_gdfs.append(shipping_exclusions[["geometry"]])

    if not all_gdfs:
        raise ValueError(
            "FAILED: No exclusion geometries to merge — "
            "all input datasets were empty after EEZ clipping"
        )

    merged = gpd.GeoDataFrame(
        pd.concat(all_gdfs, ignore_index=True),
        crs=target_crs,
    )
    logger.info(f"Merged {len(merged)} total exclusion features from all categories")

    # Reproject any stray CRS mismatches (should already be target_crs)
    if merged.crs != target_crs:
        logger.info(f"Reprojecting merged exclusions from {merged.crs} to {target_crs}")
        merged = merged.to_crs(target_crs)

    # Dissolve overlapping polygons so rasterisation counts each pixel once
    merged = dissolve_overlaps(merged)

    return merged


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD OFFSHORE EXCLUSIONS")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        input_paths = {
            "gb_eez": snk.input.gb_eez_zones,
            "marine_protected_gb": snk.input.marine_protected_gb,
            "shipping_density": snk.input.shipping_density,
            "ccs_ew": snk.input.ccs_ew,
            "ccs_sco": snk.input.ccs_sco,
            "gas_storage_gb": snk.input.gas_storage_gb,
            "og_areas_gb": snk.input.og_areas_gb,
            "marine_mining_gb": snk.input.marine_mining_gb,
            "marine_aggregates_gb": snk.input.marine_aggregates_gb,
            "historic_environment_marine": snk.input.historic_environment_marine,
            "wave_sco": snk.input.wave_sco,
            "wave_ew": snk.input.wave_ew,
            "tidal_sco": snk.input.tidal_sco,
            "tidal_ew": snk.input.tidal_ew,
        }
        output_raster = snk.output.exclusion_raster
        land_config = snk.params.config
    else:
        data_dir = "data/land/marine"
        input_paths = {
            "gb_eez": f"{data_dir}/gb_eez.gpkg",
            "marine_protected_gb": f"{data_dir}/offwind_protected_areas_gb.gpkg",
            "shipping_density": f"{data_dir}/shipping_density_eu.geotiff",
            "ccs_ew": f"{data_dir}/offshore_licensed_ccs_ew.gpkg",
            "ccs_sco": f"{data_dir}/offshore_licensed_ccs_scotland.gpkg",
            "gas_storage_gb": f"{data_dir}/offshore_gas_storage_sites_gb.gpkg",
            "og_areas_gb": f"{data_dir}/offshore_o&g_zones_gb.gpkg",
            "marine_mining_gb": f"{data_dir}/marine_mining_sites_gb.gpkg",
            "marine_aggregates_gb": f"{data_dir}/marine_aggregates_sites_gb.gpkg",
            "historic_environment_marine": f"{data_dir}/historic_environment_marine.gpkg",
            "wave_sco": f"{data_dir}/wave_licensed_sites_scotland.gpkg",
            "wave_ew": f"{data_dir}/wave_licensed_sites_ew.gpkg",
            "tidal_sco": f"{data_dir}/tidal_licensed_sites_scotland.gpkg",
            "tidal_ew": f"{data_dir}/tidal_licensed_sites_ew.gpkg",
        }
        output_raster = "resources/land/offshore_exclusions.tif"
        land_config = {}

    foundation = land_config.get("foundation", {})
    target_crs = f"EPSG:{foundation.get('target_crs', 27700)}"
    resolution = foundation.get("resolution", 100)

    # Validate inputs
    validate_inputs(input_paths)

    # Stage 1: Load GB EEZ boundary
    stage_start = time.time()
    logger.info("Stage 1: Loading GB EEZ boundary...")
    eez = load_eez(input_paths["gb_eez"], target_crs=target_crs)
    stage_times["Load EEZ boundary"] = time.time() - stage_start

    # Stage 2: Load and clip vector exclusion datasets to EEZ
    stage_start = time.time()
    logger.info("Stage 2: Loading and clipping vector exclusion datasets...")
    vector_datasets = load_and_clip_vector_datasets(
        input_paths, eez, target_crs=target_crs
    )
    stage_times["Load and clip vector datasets"] = time.time() - stage_start

    # Stage 3: Load shipping density, clip to EEZ, threshold at Q90
    stage_start = time.time()
    logger.info("Stage 3: Processing shipping density raster...")
    shipping_exclusions = load_and_clip_shipping_density(
        input_paths["shipping_density"], eez, target_crs=target_crs
    )
    stage_times["Process shipping density"] = time.time() - stage_start

    # Stage 4: Merge all exclusion geometries and dissolve overlaps
    stage_start = time.time()
    logger.info("Stage 4: Merging all exclusion geometries...")
    all_exclusions = merge_all_exclusions(
        vector_datasets, shipping_exclusions, target_crs=target_crs
    )
    stage_times["Merge and dissolve exclusions"] = time.time() - stage_start

    # Stage 5: Create reference grid and rasterise
    stage_start = time.time()
    logger.info("Stage 5: Rasterising exclusion zones to canonical GB grid...")

    width, height, transform, crs = create_reference_grid(
        resolution=resolution, crs=target_crs
    )
    template = (width, height, transform, crs)
    logger.info(f"Reference grid: {width}x{height} pixels, {resolution}m resolution")

    exclusion_raster = rasterize_vector(
        all_exclusions, template, burn_value=1, dtype="uint8"
    )
    n_excluded = int(exclusion_raster.sum())
    n_total = exclusion_raster.size
    logger.info(
        f"Exclusion raster: {n_excluded} excluded pixels "
        f"({100 * n_excluded / n_total:.2f}% of grid)"
    )
    stage_times["Rasterise exclusions"] = time.time() - stage_start

    # Stage 6: Write single-band GeoTIFF output
    stage_start = time.time()
    logger.info(f"Stage 6: Writing exclusion raster to {output_raster}...")

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "crs": target_crs,
        "transform": transform,
    }

    write_geotiff(
        exclusion_raster, profile, output_raster,
        band_names=["offshore_exclusions"],
    )
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="build_offshore_exclusions",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={"target_crs": target_crs},
    )

    logger.info("=" * 80)
    logger.info("Script Complete")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
