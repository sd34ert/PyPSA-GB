#!/usr/bin/env python3
"""
Land Use Utilities for PyPSA-GB
===============================

Shared geospatial toolbox for the land constraints pipeline. Every foundation
script in ``scripts/land/``, and the nuclear/hydrogen eligibility scripts,
imports from this module rather than duplicating vector, raster, or zonal
operations.

The module is organised into five functional groups:

Vector I/O & Manipulation
    - load_and_reproject_vector: Load a vector file and reproject to a target CRS
    - merge_national_datasets: Combine England/Scotland/Wales files into a single
      GB-wide GeoDataFrame
    - dissolve_overlaps: Union overlapping polygons within a layer so each pixel
      is counted once during rasterisation
    - buffer_geometries: Apply a spatial buffer (metres) to create exclusion zones

Rasterisation
    - get_gb_canonical_bounds: Return the canonical GB bounding box that all
      foundation rasters must share for pixel-aligned stacking
    - create_reference_grid: Build the template grid (extent, resolution, affine
      transform) that ALL rasters must match for consistent pixel alignment
    - rasterize_vector: Burn vector geometries into a binary raster (presence/absence)
    - rasterize_continuous: Burn a numeric attribute column into a continuous raster
    - reproject_raster: Reproject an existing raster to the project grid
      (nearest-neighbour for categorical, bilinear for continuous)

Output Writing
    - write_geotiff: Save a numpy array as a GeoTIFF with consistent metadata
      (LZW compression, dtype-appropriate nodata, 256x256 tiling)

Zonal Statistics
    - load_zone_shapes: Load zone boundary geometries with CRS validation
    - calculate_zone_fraction: Fraction of each zone covered by a binary mask
    - calculate_zone_summary: Compute mean/max/percentile of a continuous raster
      per zone

Validation
    - validate_crs: Assert that a dataset's CRS matches the expected EPSG code
    - validate_gb_coverage: Warn if data extent covers less than 90% of the GB
      bounding box

Notes
-----
- All raster processing uses EPSG:27700 (OSGB36 / British National Grid) as
  the project-standard projected CRS for area calculations within GB.
- ``create_reference_grid`` is the critical consistency function — every call to
  ``rasterize_vector``, ``rasterize_continuous``, or ``reproject_raster`` should
  use the same template so that outputs are pixel-aligned for stacking in
  ``calculate_zone_statistics`` and ``build_availability_matrix``.
- Foundation scripts should call ``create_reference_grid()`` with no bounds
  argument so that the canonical GB extent from ``get_gb_canonical_bounds()``
  is used automatically.
- This module has no dependency on PyPSA or Snakemake; it operates purely on
  geospatial primitives (GeoDataFrames, numpy arrays, rasterio profiles).

See Also
--------
scripts.utilities.spatial_utils : Existing spatial utilities (coordinate
    transforms, site-to-bus mapping) — complements but does not overlap with
    this module.

Author: Kate O'Neill
Date: 2026-02-20
"""

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.transform import Affine
from rasterio.warp import Resampling, calculate_default_transform, reproject

logger = logging.getLogger(__name__)

# ============================================================================
# Vector I/O & Manipulation
# ============================================================================


def load_and_reproject_vector(
    filepath: str | Path, target_crs: str = "EPSG:27700", layer: str | None = None
) -> gpd.GeoDataFrame:
    """
    Load a vector file and reproject to a target CRS.

    Parameters
    ----------
    filepath : str or Path
        Path to vector file (Shapefile, GeoPackage, GeoJSON, etc.)
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700"
    layer : str, optional
        Layer name for multi-layer formats (e.g., GeoPackage), by default None

    Returns
    -------
    gpd.GeoDataFrame
        Loaded and reprojected vector data

    Raises
    ------
    FileNotFoundError
        If the input file does not exist
    ValueError
        If the file cannot be read or has no valid geometries
    """
    filepath = Path(filepath)

    if not filepath.exists():
        raise FileNotFoundError(f"Vector file not found: {filepath}")

    try:
        gdf = gpd.read_file(filepath, layer=layer)
    except Exception as e:
        raise ValueError(f"Failed to read vector file {filepath}: {e}") from e

    if gdf.empty:
        raise ValueError(f"Vector file is empty: {filepath}")

    if gdf.crs is None:
        raise ValueError(
            f"File {filepath} has no CRS defined. Fix this by setting CRS in the source file."
        )

    if gdf.crs != target_crs:
        logger.info(f"Reprojecting {filepath.name} from {gdf.crs} to {target_crs}")
        gdf = gdf.to_crs(target_crs)

    return gdf


def merge_national_datasets(
    paths: list[str | Path],
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Combine multiple vector files into a single GB-wide GeoDataFrame.

    Loads each file with :func:`load_and_reproject_vector` (ensuring a
    common CRS) then concatenates the results.

    Parameters
    ----------
    paths : list of str or Path
        Paths to vector files (e.g. per-nation shapefiles or GB-wide
        GeoPackages). Must contain at least one path.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700"

    Returns
    -------
    gpd.GeoDataFrame
        Combined GeoDataFrame with all features in ``target_crs``

    Raises
    ------
    ValueError
        If ``paths`` is empty or any file fails to load
    """
    if not paths:
        raise ValueError("paths list must contain at least one file path.")

    gdfs = []
    for p in paths:
        gdf = load_and_reproject_vector(p, target_crs=target_crs)
        gdfs.append(gdf)
        logger.info(f"Loaded {len(gdf)} features from {Path(p).name}")

    combined_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
    logger.info(f"Merged {len(paths)} datasets into {len(combined_gdf)} total features.")

    return combined_gdf


def buffer_geometries(
    gdf: gpd.GeoDataFrame,
    distance_m: float,
) -> gpd.GeoDataFrame:
    """
    Apply a spatial buffer to create exclusion zones.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Input GeoDataFrame with geometries to buffer
    distance_m : float
        Buffer distance in metres. Must be non-negative.

    Returns
    -------
    gpd.GeoDataFrame
        GeoDataFrame with buffered geometries

    Raises
    ------
    ValueError
        If CRS uses geographic (degree) units or distance is negative
    """
    if distance_m < 0:
        raise ValueError(f"Buffer distance must be non-negative, got {distance_m}")

    if gdf.empty or distance_m == 0:
        return gdf

    if gdf.crs is None or not gdf.crs.is_projected:
        raise ValueError(
            f"Buffer requires a projected CRS (metre units), "
            f"got {gdf.crs}. Reproject before buffering."
        )

    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.buffer(distance_m)
    logger.info(f"Buffered {len(gdf)} geometries by {distance_m} m.")

    return gdf


def dissolve_overlaps(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Union overlapping polygons so each pixel is counted once during rasterisation.

    Dissolves all geometries into a single MultiPolygon, then explodes back
    to individual polygons.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Input GeoDataFrame with potentially overlapping polygons

    Returns
    -------
    gpd.GeoDataFrame
        GeoDataFrame with non-overlapping polygons in the same CRS
    """
    if gdf.empty:
        return gdf

    dissolved = gdf.dissolve()
    exploded = dissolved.explode(index_parts=False).reset_index(drop=True)

    logger.info(f"Dissolved {len(gdf)} features into {len(exploded)} non-overlapping polygons.")

    return exploded


# ============================================================================
# Rasterisation
# ============================================================================


def create_reference_grid(
    bounds: tuple[float, float, float, float] | None = None,
    resolution: float = 100,
    crs: str = "EPSG:27700",
) -> tuple[int, int, Affine, str]:
    """
    Build the template grid that ALL rasters must match for consistent pixel alignment.

    Every call to :func:`rasterize_vector`, :func:`rasterize_continuous`, or
    :func:`reproject_raster` should use the same template returned by this
    function so that outputs are pixel-aligned for stacking.

    When *bounds* is ``None`` (the default), the canonical GB bounding box
    from :func:`get_gb_canonical_bounds` is used.  **All foundation raster
    scripts should omit bounds** to ensure every output shares the same
    grid.  Pass explicit bounds only for unit tests or non-GB data.

    Parameters
    ----------
    bounds : tuple of float or None, optional
        Spatial extent as ``(xmin, ymin, xmax, ymax)`` in the units of *crs*.
        If ``None``, uses the canonical GB bounds for the given *crs*.
    resolution : float, optional
        Pixel size in CRS units (metres for EPSG:27700). Must be positive.
        By default 100.
    crs : str, optional
        Coordinate reference system, by default "EPSG:27700"

    Returns
    -------
    tuple of (int, int, Affine, str)
        ``(width, height, transform, crs)`` where *width* and *height* are
        pixel counts, *transform* is the affine mapping from pixel to
        real-world coordinates, and *crs* is passed through unchanged.

    Raises
    ------
    ValueError
        If *resolution* is not positive or *bounds* are invalid.
    """
    if bounds is None:
        bounds = get_gb_canonical_bounds(crs=crs)
        logger.info("Using canonical GB bounds for reference grid.")

    if resolution <= 0:
        raise ValueError(f"Resolution must be positive, got {resolution}")

    xmin, ymin, xmax, ymax = bounds

    if xmin >= xmax or ymin >= ymax:
        raise ValueError(
            f"Invalid bounds: xmin ({xmin}) must be < xmax ({xmax}) "
            f"and ymin ({ymin}) must be < ymax ({ymax})"
        )

    width = int((xmax - xmin) / resolution)
    height = int((ymax - ymin) / resolution)

    transform = Affine.translation(xmin, ymax) * Affine.scale(resolution, -resolution)

    logger.info(f"Reference grid: {width}x{height} pixels, resolution={resolution}, crs={crs}")

    return width, height, transform, crs


def rasterize_vector(
    gdf: gpd.GeoDataFrame,
    template: tuple[int, int, Affine, str],
    burn_value: int = 1,
    dtype: str = "uint8",
) -> np.ndarray:
    """
    Burn vector geometries into a binary raster (presence/absence).

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Input geometries to rasterize.
    template : tuple of (int, int, Affine, str)
        Reference grid from :func:`create_reference_grid` as
        ``(width, height, transform, crs)``.
    burn_value : int, optional
        Value to assign to pixels covered by geometries, by default 1.
    dtype : str, optional
        NumPy dtype for the output array, by default "uint8".

    Returns
    -------
    np.ndarray
        2-D raster array of shape ``(height, width)`` with *burn_value*
        where geometries are present and 0 elsewhere.
    """
    width, height, transform, _crs = template

    if gdf.empty:
        logger.warning("Empty GeoDataFrame passed to rasterize_vector; returning zeros.")
        return np.zeros((height, width), dtype=dtype)

    rasterized = features.rasterize(
        shapes=[(geom, burn_value) for geom in gdf.geometry],
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype=dtype,
    )

    logger.info(
        f"Rasterized {len(gdf)} geometries: "
        f"{np.count_nonzero(rasterized)} of {rasterized.size} pixels burned."
    )

    return rasterized


def rasterize_continuous(
    gdf: gpd.GeoDataFrame,
    template: tuple[int, int, Affine, str],
    value_column: str,
    dtype: str = "float32",
    nodata: float = -9999.0,
) -> np.ndarray:
    """
    Burn a numeric attribute column into a continuous raster.

    Works like :func:`rasterize_vector` but each geometry is burned with
    the value from *value_column* rather than a fixed burn value.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        Input geometries with a numeric column to rasterize.
    template : tuple of (int, int, Affine, str)
        Reference grid from :func:`create_reference_grid` as
        ``(width, height, transform, crs)``.
    value_column : str
        Name of the numeric column in *gdf* to burn into the raster.
    dtype : str, optional
        NumPy dtype for the output array, by default "float32".
    nodata : float, optional
        Fill value for pixels not covered by any geometry, by default -9999.0.

    Returns
    -------
    np.ndarray
        2-D raster array of shape ``(height, width)`` with attribute values
        where geometries are present and *nodata* elsewhere.

    Raises
    ------
    KeyError
        If *value_column* is not present in *gdf*.
    """
    if value_column not in gdf.columns:
        raise KeyError(
            f"Column '{value_column}' not found in GeoDataFrame. "
            f"Available columns: {list(gdf.columns)}"
        )

    width, height, transform, _crs = template

    if gdf.empty:
        logger.warning("Empty GeoDataFrame passed to rasterize_continuous; returning nodata.")
        arr = np.full((height, width), nodata, dtype=dtype)
        return arr

    shapes = [(geom, value) for geom, value in zip(gdf.geometry, gdf[value_column])]

    rasterized = features.rasterize(
        shapes=shapes,
        out_shape=(height, width),
        transform=transform,
        fill=nodata,
        dtype=dtype,
    )

    valid_pixels = np.count_nonzero(rasterized != nodata)
    logger.info(
        f"Rasterized '{value_column}' from {len(gdf)} geometries: "
        f"{valid_pixels} of {rasterized.size} pixels have data."
    )

    return rasterized


def reproject_raster(
    src_path: str | Path,
    target_crs: str = "EPSG:27700",
    resolution: float | None = None,
    resampling: str = "nearest",
) -> tuple[np.ndarray, dict]:
    """
    Reproject an existing raster to the project grid.

    Opens a source raster, computes the destination transform for the
    *target_crs* and *resolution*, then warps the data using the specified
    *resampling* method.

    Parameters
    ----------
    src_path : str or Path
        Path to the source raster file (GeoTIFF, etc.).
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".
    resolution : float or None, optional
        Target pixel size in CRS units (metres for EPSG:27700). If ``None``,
        the source resolution is preserved.
    resampling : str, optional
        Resampling method name — ``"nearest"`` for categorical data,
        ``"bilinear"`` for continuous data. Must be a valid
        ``rasterio.warp.Resampling`` member name. By default ``"nearest"``.

    Returns
    -------
    tuple of (np.ndarray, dict)
        ``(dst_array, dst_profile)`` where *dst_array* has shape
        ``(bands, height, width)`` and *dst_profile* is a rasterio profile
        dict suitable for :func:`write_geotiff`.

    Raises
    ------
    FileNotFoundError
        If *src_path* does not exist.
    ValueError
        If *resampling* is not a valid ``Resampling`` member name.
    """
    src_path = Path(src_path)
    if not src_path.exists():
        raise FileNotFoundError(f"Raster file not found: {src_path}")

    try:
        resample_method = Resampling[resampling]
    except KeyError as err:
        valid = [r.name for r in Resampling]
        raise ValueError(
            f"Unknown resampling method '{resampling}'. Valid options: {valid}"
        ) from err

    with rasterio.open(src_path) as src:
        src_width, src_height = src.width, src.height
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src.crs,
            target_crs,
            src_width,
            src_height,
            *src.bounds,
            resolution=resolution,
        )

        dst_profile = src.profile.copy()
        dst_profile.update(
            crs=target_crs,
            transform=dst_transform,
            width=dst_width,
            height=dst_height,
        )

        dst_array = np.empty((src.count, dst_height, dst_width), dtype=src.dtypes[0])

        for band_idx in range(1, src.count + 1):
            reproject(
                source=rasterio.band(src, band_idx),
                destination=dst_array[band_idx - 1],
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=target_crs,
                resampling=resample_method,
            )

    logger.info(
        f"Reprojected {src_path.name}: {src_width}x{src_height} -> "
        f"{dst_width}x{dst_height}, crs={target_crs}, "
        f"resampling={resampling}"
    )

    return dst_array, dst_profile


# ============================================================================
# Output Writing
# ============================================================================

# Default nodata values by dtype, chosen to be valid within each type's range.
_NODATA_DEFAULTS = {
    "uint8": 255,
    "uint16": 65535,
    "int16": -9999,
    "int32": -9999,
    "float32": -9999.0,
    "float64": -9999.0,
}


def write_geotiff(
    array: np.ndarray,
    profile: dict,
    path: str | Path,
    band_names: list[str] | None = None,
    nodata: float | None = None,
) -> None:
    """
    Save a numpy array as a GeoTIFF with consistent metadata.

    Applies standard settings (LZW compression, 256x256 tiling) and a
    dtype-appropriate nodata value so that all raster outputs are uniform.

    Parameters
    ----------
    array : np.ndarray
        Raster data. Either 2-D ``(height, width)`` for a single band or
        3-D ``(bands, height, width)`` for multi-band output.
    profile : dict
        Rasterio profile dict (must include at least ``crs``, ``transform``,
        ``width``, ``height``, and ``dtype``).
    path : str or Path
        Output file path for the GeoTIFF.
    band_names : list of str, optional
        Descriptive names for each band (e.g. ``['Natura2000', 'SSSI']``).
        Length must match the band count. If ``None``, no descriptions are
        added.
    nodata : float or None, optional
        Value used to represent missing data. If ``None`` (the default), a
        dtype-appropriate value is chosen automatically (e.g. 255 for uint8,
        -9999 for float32). Pass an explicit value to override.

    Raises
    ------
    ValueError
        If *band_names* length does not match the number of bands in *array*.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if array.ndim == 2:
        array = array[np.newaxis, ...]  # (1, height, width)

    band_count = array.shape[0]

    if band_names is not None and len(band_names) != band_count:
        raise ValueError(
            f"band_names length ({len(band_names)}) does not match band count ({band_count})"
        )

    if nodata is None:
        dtype = profile.get("dtype", str(array.dtype))
        nodata = _NODATA_DEFAULTS.get(str(dtype), -9999)

    profile = profile.copy()
    profile.update(
        driver="GTiff",
        count=band_count,
        compress="lzw",
        nodata=nodata,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )

    with rasterio.open(path, "w", **profile) as dst:
        for i in range(band_count):
            dst.write(array[i], i + 1)

        if band_names is not None:
            for i, name in enumerate(band_names):
                dst.set_band_description(i + 1, name)

    logger.info(f"Wrote GeoTIFF: {path} ({band_count} band(s))")


# ============================================================================
# Zonal Statistics
# ============================================================================


def load_zone_shapes(
    path: str | Path,
    target_crs: str = "EPSG:27700",
) -> gpd.GeoDataFrame:
    """
    Load zone boundary geometries with CRS validation.

    Loads a vector file containing zone boundaries, reprojects to
    *target_crs*, and validates that a recognisable zone-name column
    is present.

    Parameters
    ----------
    path : str or Path
        Path to vector file containing zone boundary polygons.
    target_crs : str, optional
        Target coordinate reference system, by default "EPSG:27700".

    Returns
    -------
    gpd.GeoDataFrame
        Zone boundaries reprojected to *target_crs* with a ``zone_name``
        column identifying each zone.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the file has no recognisable zone-name column.
    """
    gdf = load_and_reproject_vector(path, target_crs=target_crs)

    # Identify the zone-name column (datasets use different conventions)
    name_candidates = ["zone_name", "zone", "Name_1", "name", "Name", "ZONE", "NAME"]
    zone_col = None
    for col in name_candidates:
        if col in gdf.columns:
            zone_col = col
            break

    if zone_col is None:
        raise ValueError(
            f"No recognisable zone-name column in {path}. "
            f"Expected one of {name_candidates}, "
            f"found columns: {list(gdf.columns)}"
        )

    # Normalise to a consistent column name
    if zone_col != "zone_name":
        gdf = gdf.rename(columns={zone_col: "zone_name"})
        logger.info(f"Renamed column '{zone_col}' to 'zone_name'.")

    logger.info(f"Loaded {len(gdf)} zone shapes from {Path(path).name}.")

    return gdf


def calculate_zone_fraction(
    raster: np.ndarray,
    zones: gpd.GeoDataFrame,
    transform: Affine,
) -> pd.Series:
    """
    Fraction of each zone covered by a binary mask.

    For each zone polygon, counts the fraction of pixels with non-zero
    values relative to the total number of pixels within that zone.

    Parameters
    ----------
    raster : np.ndarray
        2-D binary raster array of shape ``(height, width)`` where non-zero
        pixels indicate presence (e.g. protected area, flood risk).
    zones : gpd.GeoDataFrame
        Zone boundaries with a ``zone_name`` column (as returned by
        :func:`load_zone_shapes`).
    transform : Affine
        Affine transform mapping pixel coordinates to CRS coordinates,
        matching the *raster* array.

    Returns
    -------
    pd.Series
        Fraction of each zone covered by the mask, indexed by zone name.
        Values are in the range [0.0, 1.0].
    """
    fractions = {}

    for _, zone in zones.iterrows():
        # Create a boolean mask for this zone (True = inside zone)
        zone_mask = features.geometry_mask(
            [zone.geometry],
            out_shape=raster.shape,
            transform=transform,
            invert=True,
        )

        total_pixels = zone_mask.sum()
        if total_pixels == 0:
            fractions[zone["zone_name"]] = 0.0
            continue

        covered_pixels = np.count_nonzero(raster[zone_mask])
        fractions[zone["zone_name"]] = covered_pixels / total_pixels

    result = pd.Series(fractions, name="fraction")
    logger.info(
        f"Calculated zone fractions for {len(zones)} zones: "
        f"mean={result.mean():.3f}, max={result.max():.3f}"
    )

    return result


def calculate_zone_summary(
    raster: np.ndarray,
    zones: gpd.GeoDataFrame,
    transform: Affine,
    stats: list[str],
    nodata: float = -9999.0,
) -> pd.DataFrame:
    """
    Compute summary statistics of a continuous raster per zone.

    For each zone polygon, extracts raster values (excluding *nodata*) and
    computes the requested statistics.

    Parameters
    ----------
    raster : np.ndarray
        2-D continuous raster array of shape ``(height, width)``.
    zones : gpd.GeoDataFrame
        Zone boundaries with a ``zone_name`` column (as returned by
        :func:`load_zone_shapes`).
    transform : Affine
        Affine transform mapping pixel coordinates to CRS coordinates,
        matching the *raster* array.
    stats : list of str
        Statistics to compute. Supported values: ``"mean"``, ``"max"``,
        ``"min"``, ``"median"``, ``"std"``, and percentiles as ``"p5"``,
        ``"p10"``, ..., ``"p95"``.
    nodata : float, optional
        Value to exclude from calculations, by default -9999.0.

    Returns
    -------
    pd.DataFrame
        One row per zone with columns ``zone_name`` plus one column per
        requested statistic.

    Raises
    ------
    ValueError
        If *stats* is empty or contains an unrecognised statistic name.
    """
    if not stats:
        raise ValueError("stats list must contain at least one statistic.")

    _simple = {"mean", "max", "min", "median", "std"}
    for s in stats:
        if s not in _simple and not (s.startswith("p") and s[1:].isdigit()):
            raise ValueError(
                f"Unrecognised statistic '{s}'. "
                f"Supported: {sorted(_simple)} and percentiles (p5..p95)."
            )

    records = []

    for _, zone in zones.iterrows():
        zone_mask = features.geometry_mask(
            [zone.geometry],
            out_shape=raster.shape,
            transform=transform,
            invert=True,
        )

        values = raster[zone_mask]
        values = values[values != nodata]

        row = {"zone_name": zone["zone_name"]}

        if values.size == 0:
            for s in stats:
                row[s] = np.nan
        else:
            for s in stats:
                if s == "mean":
                    row[s] = float(np.mean(values))
                elif s == "max":
                    row[s] = float(np.max(values))
                elif s == "min":
                    row[s] = float(np.min(values))
                elif s == "median":
                    row[s] = float(np.median(values))
                elif s == "std":
                    row[s] = float(np.std(values))
                else:
                    # Percentile: "p95" -> 95
                    pct = int(s[1:])
                    row[s] = float(np.percentile(values, pct))

        records.append(row)

    df = pd.DataFrame(records)
    logger.info(f"Calculated zone summary ({', '.join(stats)}) for {len(zones)} zones.")

    return df


# ============================================================================
# Validation
# ============================================================================


def validate_crs(
    data: gpd.GeoDataFrame | dict,
    expected_crs: str = "EPSG:27700",
) -> None:
    """
    Assert that a dataset's CRS matches the expected EPSG code.

    Parameters
    ----------
    data : gpd.GeoDataFrame or dict
        A GeoDataFrame (checked via ``.crs``) or a rasterio profile dict
        (checked via ``"crs"`` key).
    expected_crs : str, optional
        Expected CRS string, by default "EPSG:27700".

    Raises
    ------
    ValueError
        If the CRS is missing or does not match *expected_crs*.
    """
    from pyproj import CRS

    expected = CRS.from_user_input(expected_crs)

    if isinstance(data, gpd.GeoDataFrame):
        if data.crs is None:
            raise ValueError("GeoDataFrame has no CRS defined.")
        actual = CRS.from_user_input(data.crs)
    elif isinstance(data, dict):
        if "crs" not in data or data["crs"] is None:
            raise ValueError("Rasterio profile has no 'crs' key.")
        actual = CRS.from_user_input(data["crs"])
    else:
        raise TypeError(f"Expected GeoDataFrame or dict, got {type(data).__name__}")

    if actual != expected:
        raise ValueError(f"CRS mismatch: expected {expected_crs}, got {actual.to_epsg() or actual}")

    logger.debug(f"CRS validation passed: {expected_crs}")


# Canonical GB bounding box in EPSG:27700 (OSGB36 / British National Grid).
# Covers mainland Great Britain from SW Cornwall to N Scotland with a small
# margin.  Every raster produced by the land constraints pipeline MUST use
# these bounds (via ``get_gb_canonical_bounds`` / ``create_reference_grid``)
# so that all outputs are pixel-aligned for stacking.
_GB_BOUNDS_27700 = (0, 0, 700_000, 1_300_000)


def get_gb_canonical_bounds(
    crs: str = "EPSG:27700",
) -> tuple[float, float, float, float]:
    """
    Return the canonical Great Britain bounding box for raster processing.

    All foundation raster scripts must use these bounds so that every output
    shares identical extent, resolution, and cell alignment.  This is the
    single source of truth for the raster grid extent used throughout the
    land constraints pipeline.

    Parameters
    ----------
    crs : str, optional
        Coordinate reference system.  Currently only ``"EPSG:27700"`` is
        supported.  By default ``"EPSG:27700"``.

    Returns
    -------
    tuple of float
        ``(xmin, ymin, xmax, ymax)`` in CRS units (metres for EPSG:27700).

    Raises
    ------
    ValueError
        If *crs* is not ``"EPSG:27700"``.
    """
    if crs != "EPSG:27700":
        raise ValueError(
            f"Canonical GB bounds are only defined for EPSG:27700, got {crs}. "
            "Reproject data to EPSG:27700 before creating the reference grid."
        )
    return _GB_BOUNDS_27700


def validate_gb_coverage(
    data: gpd.GeoDataFrame | tuple[float, float, float, float],
    gb_bounds: tuple[float, float, float, float] = _GB_BOUNDS_27700,
    min_fraction: float = 0.90,
) -> bool:
    """
    Warn if data extent covers less than *min_fraction* of the GB bounding box.

    Computes the ratio of the intersection area between the data extent and
    *gb_bounds* to the total *gb_bounds* area. If coverage is below
    *min_fraction*, a warning is logged and ``False`` is returned.

    Parameters
    ----------
    data : gpd.GeoDataFrame or tuple of float
        Either a GeoDataFrame (whose ``total_bounds`` are used) or an
        ``(xmin, ymin, xmax, ymax)`` tuple in CRS-native units.
    gb_bounds : tuple of float, optional
        Reference GB bounding box as ``(xmin, ymin, xmax, ymax)`` in
        EPSG:27700 units (metres). By default covers mainland GB.
    min_fraction : float, optional
        Minimum acceptable coverage fraction, by default 0.90.

    Returns
    -------
    bool
        ``True`` if the data extent covers at least *min_fraction* of GB,
        ``False`` otherwise.
    """
    if isinstance(data, gpd.GeoDataFrame):
        if data.empty:
            logger.warning("Empty GeoDataFrame — cannot assess GB coverage.")
            return False
        dxmin, dymin, dxmax, dymax = data.total_bounds
    elif isinstance(data, (tuple, list)) and len(data) == 4:
        dxmin, dymin, dxmax, dymax = data
    else:
        raise TypeError(
            f"Expected GeoDataFrame or (xmin, ymin, xmax, ymax) tuple, got {type(data).__name__}"
        )

    gxmin, gymin, gxmax, gymax = gb_bounds

    # Intersection of the two boxes
    ixmin = max(dxmin, gxmin)
    iymin = max(dymin, gymin)
    ixmax = min(dxmax, gxmax)
    iymax = min(dymax, gymax)

    if ixmin >= ixmax or iymin >= iymax:
        logger.warning("Data extent does not overlap the GB bounding box at all.")
        return False

    intersection_area = (ixmax - ixmin) * (iymax - iymin)
    gb_area = (gxmax - gxmin) * (gymax - gymin)
    fraction = intersection_area / gb_area

    if fraction < min_fraction:
        logger.warning(
            f"Data covers only {fraction:.1%} of the GB bounding box "
            f"(minimum expected: {min_fraction:.0%}). "
            f"Data bounds: ({dxmin:.0f}, {dymin:.0f}, {dxmax:.0f}, {dymax:.0f}), "
            f"GB bounds: ({gxmin:.0f}, {gymin:.0f}, {gxmax:.0f}, {gymax:.0f})."
        )
        return False

    logger.debug(f"GB coverage check passed: {fraction:.1%}")
    return True


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python land_utils.py <vector_file> [target_crs]")
        sys.exit(1)

    vector_file_path = sys.argv[1]
    target_crs = sys.argv[2] if len(sys.argv) > 2 else "EPSG:27700"

    try:
        gdf = load_and_reproject_vector(vector_file_path, target_crs=target_crs)
        print(f"CRS: {gdf.crs}")
        print(f"Shape: {gdf.shape}")
        print(gdf.head())
    except (FileNotFoundError, ValueError) as e:
        print(e)
        sys.exit(1)
