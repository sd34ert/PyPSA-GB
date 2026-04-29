"""
Unit tests for build_offshore_exclusions.py

Tests the offshore exclusion raster building pipeline:
- Input validation (missing files raise errors)
- EEZ loading and dissolve
- Vector clipping to EEZ boundary
- Shipping density Q90 threshold and polygonisation
- Merge and dissolve of all exclusion categories
- Binary rasterization (1 = excluded, 0 = available)
- GeoTIFF write/read round-trip
- Edge cases (empty geometries, overlapping sources, features outside EEZ)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_offshore_exclusions import (
    clip_vector_to_eez,
    load_and_clip_shipping_density,
    merge_all_exclusions,
    validate_inputs,
)
from scripts.utilities.land_utils import (
    create_reference_grid,
    rasterize_vector,
    write_geotiff,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test rasters (1km x 1km in EPSG:27700)."""
    return (400_000, 300_000, 401_000, 301_000)


@pytest.fixture
def eez_gdf(small_bounds):
    """Synthetic EEZ boundary covering the test area as a dissolved GeoDataFrame."""
    xmin, ymin, xmax, ymax = small_bounds
    geom = box(xmin, ymin, xmax, ymax)
    return gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")


@pytest.fixture
def exclusion_gpkgs(tmp_path, small_bounds):
    """
    Create synthetic vector exclusion GeoPackages for all 11 vector inputs.

    Each category gets a polygon in a distinct sub-region of the test area
    so they are distinguishable after merge. All in EPSG:27700.

    Returns a dict with the expected input_paths keys and file paths.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    geometries = {
        "marine_protected_gb": box(xmin, mid_y, mid_x, ymax),
        "gas_storage_gb": box(mid_x, mid_y, xmax, ymax),
        "og_areas_gb": box(xmin, ymin, mid_x, mid_y),
        "marine_mining_gb": box(mid_x, ymin, xmax, mid_y),
        "marine_aggregates_gb": box(xmin + 100, ymin + 100, mid_x - 100, mid_y - 100),
        "ccs_sco": box(xmin, mid_y + 100, mid_x - 200, ymax - 100),
        "ccs_ew": box(mid_x + 100, mid_y + 100, xmax - 100, ymax - 100),
        "wave_sco": box(xmin + 50, ymin + 50, xmin + 200, ymin + 200),
        "wave_ew": box(mid_x + 50, ymin + 50, mid_x + 200, ymin + 200),
        "tidal_sco": box(xmax - 200, ymax - 200, xmax - 50, ymax - 50),
        "tidal_ew": box(mid_x - 200, ymax - 200, mid_x - 50, ymax - 50),
        "historic_environment_marine": box(xmin + 300, ymin + 300, xmin + 500, ymin + 500),
    }

    paths = {}
    for name, geom in geometries.items():
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
        path = tmp_path / f"{name}.gpkg"
        gdf.to_file(path, driver="GPKG")
        paths[name] = str(path)

    return paths


@pytest.fixture
def shipping_raster(tmp_path, small_bounds):
    """
    Create a synthetic shipping density GeoTIFF for testing Q90 threshold.

    Produces a 10x10 pixel raster within the test area. Most pixels have
    low density (1-5), with a few high-density pixels (90-100) that should
    exceed the Q90 threshold.
    """
    xmin, ymin, xmax, ymax = small_bounds
    width, height = 10, 10
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    # Create density data: mostly low, with a high-density cluster
    data = np.ones((1, height, width), dtype="float32") * 2.0
    # Top-right 2x2 block gets high density
    data[0, 0:2, 8:10] = 95.0
    # One more high pixel
    data[0, 5, 5] = 92.0

    path = tmp_path / "shipping_density.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def vector_datasets(small_bounds):
    """Create a list of GeoDataFrames simulating clipped vector exclusion data."""
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    gdf1 = gpd.GeoDataFrame(
        geometry=[box(xmin, mid_y, mid_x, ymax)],
        crs="EPSG:27700",
    )
    gdf2 = gpd.GeoDataFrame(
        geometry=[box(mid_x, ymin, xmax, mid_y)],
        crs="EPSG:27700",
    )
    return [gdf1, gdf2]


@pytest.fixture
def shipping_polygons(small_bounds):
    """Create synthetic shipping exclusion polygons."""
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    return gpd.GeoDataFrame(
        geometry=[box(mid_x, mid_y, xmax, ymax)],
        crs="EPSG:27700",
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, exclusion_gpkgs):
        """Test that validation passes when all files exist."""
        validate_inputs(exclusion_gpkgs)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "gb_eez": str(tmp_path / "nonexistent.gpkg"),
            "marine_protected_gb": str(tmp_path / "also_missing.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, exclusion_gpkgs, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = exclusion_gpkgs.copy()
        paths["extra_missing"] = str(tmp_path / "nope.gpkg")

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)

    def test_validate_inputs_all_missing(self, tmp_path):
        """Test error message when all 14 input files are missing."""
        paths = {
            name: str(tmp_path / f"{name}.gpkg")
            for name in [
                "gb_eez", "marine_protected_gb", "shipping_density",
                "ccs_ew", "ccs_sco", "gas_storage_gb", "og_areas_gb",
                "marine_mining_gb", "marine_aggregates_gb",
                "historic_environment_marine",
                "wave_sco", "wave_ew", "tidal_sco", "tidal_ew",
            ]
        }

        with pytest.raises(FileNotFoundError, match="Missing 14 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: EEZ Clipping
# ══════════════════════════════════════════════════════════════════════════════


class TestClipVectorToEEZ:
    """Test spatial clipping of vector datasets to EEZ boundary."""

    def test_returns_geodataframe(self, eez_gdf, small_bounds):
        """Test that clipping returns a GeoDataFrame."""
        gdf = gpd.GeoDataFrame(
            geometry=[box(*small_bounds)], crs="EPSG:27700"
        )

        result = clip_vector_to_eez(gdf, eez_gdf, "test")

        assert isinstance(result, gpd.GeoDataFrame)

    def test_feature_inside_eez_retained(self, eez_gdf, small_bounds):
        """Test that features fully inside EEZ are retained."""
        xmin, ymin, xmax, ymax = small_bounds
        inner = box(xmin + 100, ymin + 100, xmax - 100, ymax - 100)
        gdf = gpd.GeoDataFrame(geometry=[inner], crs="EPSG:27700")

        result = clip_vector_to_eez(gdf, eez_gdf, "inner")

        assert len(result) == 1
        assert not result.geometry.is_empty.any()

    def test_feature_outside_eez_removed(self, eez_gdf, small_bounds):
        """Test that features entirely outside EEZ are removed."""
        xmin, ymin, xmax, ymax = small_bounds
        outside = box(xmax + 1000, ymax + 1000, xmax + 2000, ymax + 2000)
        gdf = gpd.GeoDataFrame(geometry=[outside], crs="EPSG:27700")

        result = clip_vector_to_eez(gdf, eez_gdf, "outside")

        assert len(result) == 0

    def test_feature_partially_inside_clipped(self, eez_gdf, small_bounds):
        """Test that features partially overlapping EEZ are clipped."""
        xmin, ymin, xmax, ymax = small_bounds
        # Polygon extends 500m beyond EEZ on the right
        partial = box(xmax - 200, ymin + 100, xmax + 500, ymax - 100)
        gdf = gpd.GeoDataFrame(geometry=[partial], crs="EPSG:27700")

        result = clip_vector_to_eez(gdf, eez_gdf, "partial")

        assert len(result) == 1
        # Clipped geometry should be smaller than original
        assert result.geometry.iloc[0].area < partial.area

    def test_preserves_crs(self, eez_gdf, small_bounds):
        """Test that output CRS matches input."""
        gdf = gpd.GeoDataFrame(
            geometry=[box(*small_bounds)], crs="EPSG:27700"
        )

        result = clip_vector_to_eez(gdf, eez_gdf, "crs_test")

        assert result.crs.to_epsg() == 27700


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Shipping Density Processing
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadAndClipShippingDensity:
    """Test shipping density raster loading, EEZ clipping, and Q90 threshold."""

    def test_returns_geodataframe(self, shipping_raster, eez_gdf):
        """Test that function returns a GeoDataFrame."""
        result = load_and_clip_shipping_density(
            shipping_raster, eez_gdf, target_crs="EPSG:27700"
        )

        assert isinstance(result, gpd.GeoDataFrame)

    def test_crs_is_target(self, shipping_raster, eez_gdf):
        """Test that output CRS matches target."""
        result = load_and_clip_shipping_density(
            shipping_raster, eez_gdf, target_crs="EPSG:27700"
        )

        assert result.crs.to_epsg() == 27700

    def test_produces_exclusion_polygons(self, shipping_raster, eez_gdf):
        """Test that high-density areas produce exclusion polygons."""
        result = load_and_clip_shipping_density(
            shipping_raster, eez_gdf, target_crs="EPSG:27700"
        )

        assert len(result) > 0

    def test_geometries_not_empty(self, shipping_raster, eez_gdf):
        """Test that output geometries are valid and non-empty."""
        result = load_and_clip_shipping_density(
            shipping_raster, eez_gdf, target_crs="EPSG:27700"
        )

        assert result.geometry.notna().all()
        assert not result.geometry.is_empty.any()

    def test_zero_density_returns_empty(self, tmp_path, eez_gdf, small_bounds):
        """Test that all-zero shipping density returns empty GeoDataFrame."""
        xmin, ymin, xmax, ymax = small_bounds
        width, height = 10, 10
        transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

        data = np.zeros((1, height, width), dtype="float32")
        path = tmp_path / "zero_shipping.tif"
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": width,
            "height": height,
            "count": 1,
            "crs": "EPSG:27700",
            "transform": transform,
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(data)

        result = load_and_clip_shipping_density(
            str(path), eez_gdf, target_crs="EPSG:27700"
        )

        assert result.empty


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Merge All Exclusions
# ══════════════════════════════════════════════════════════════════════════════


class TestMergeAllExclusions:
    """Test merging and dissolving all exclusion categories."""

    def test_returns_geodataframe(self, vector_datasets, shipping_polygons):
        """Test that merge returns a GeoDataFrame."""
        result = merge_all_exclusions(vector_datasets, shipping_polygons)

        assert isinstance(result, gpd.GeoDataFrame)

    def test_crs_is_epsg_27700(self, vector_datasets, shipping_polygons):
        """Test that output CRS is EPSG:27700."""
        result = merge_all_exclusions(vector_datasets, shipping_polygons)

        assert result.crs.to_epsg() == 27700

    def test_geometries_not_empty(self, vector_datasets, shipping_polygons):
        """Test that merged geometries are valid and non-empty."""
        result = merge_all_exclusions(vector_datasets, shipping_polygons)

        assert result.geometry.notna().all()
        assert not result.geometry.is_empty.any()

    def test_empty_shipping_still_merges_vectors(self, vector_datasets):
        """Test that merge works with empty shipping exclusions."""
        empty_shipping = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")

        result = merge_all_exclusions(vector_datasets, empty_shipping)

        assert len(result) > 0

    def test_overlapping_categories_dissolved(self, small_bounds):
        """Test that overlapping polygons are dissolved correctly."""
        geom = box(*small_bounds)
        gdf1 = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
        gdf2 = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
        empty_shipping = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")

        result = merge_all_exclusions([gdf1, gdf2], empty_shipping)

        # After dissolve, two identical polygons should become one
        assert len(result) == 1

    def test_all_empty_raises_valueerror(self):
        """Test that merge raises ValueError when all inputs are empty."""
        empty1 = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        empty2 = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")

        with pytest.raises(ValueError, match="No exclusion geometries to merge"):
            merge_all_exclusions([empty1], empty2)

    def test_extra_columns_stripped(self, small_bounds):
        """Test that non-geometry columns don't cause schema conflicts."""
        geom = box(*small_bounds)
        gdf1 = gpd.GeoDataFrame(
            {"name": ["MPA"], "geometry": [geom]}, crs="EPSG:27700"
        )
        gdf2 = gpd.GeoDataFrame(
            {"licence_id": [42], "geometry": [geom]}, crs="EPSG:27700"
        )
        empty_shipping = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")

        # Should not raise despite different column schemas
        result = merge_all_exclusions([gdf1, gdf2], empty_shipping)

        assert len(result) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Rasterisation and Output
# ══════════════════════════════════════════════════════════════════════════════


class TestRasteriseAndOutput:
    """Test rasterisation of merged exclusions and GeoTIFF output."""

    @pytest.fixture
    def merged_exclusions(self, vector_datasets, shipping_polygons):
        """Create merged exclusions via the production function."""
        return merge_all_exclusions(vector_datasets, shipping_polygons)

    def test_raster_is_2d_uint8(self, merged_exclusions):
        """Test that rasterised output is a 2D uint8 array."""
        width, height, transform, crs = create_reference_grid(
            resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)

        raster = rasterize_vector(merged_exclusions, template, burn_value=1, dtype="uint8")

        assert raster.ndim == 2
        assert raster.dtype == np.uint8

    def test_binary_values_only(self, merged_exclusions):
        """Test that raster contains only 0 and 1 values."""
        width, height, transform, crs = create_reference_grid(
            resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)

        raster = rasterize_vector(merged_exclusions, template, burn_value=1, dtype="uint8")

        assert set(np.unique(raster)).issubset({0, 1})

    def test_has_excluded_pixels(self, merged_exclusions):
        """Test that raster has at least some excluded pixels."""
        width, height, transform, crs = create_reference_grid(
            resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)

        raster = rasterize_vector(merged_exclusions, template, burn_value=1, dtype="uint8")

        assert np.count_nonzero(raster) > 0

    def test_has_available_pixels(self, merged_exclusions):
        """Test that raster has non-excluded pixels (not fully covered)."""
        width, height, transform, crs = create_reference_grid(
            resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)

        raster = rasterize_vector(merged_exclusions, template, burn_value=1, dtype="uint8")

        # Canonical GB grid is much larger than our test polygons
        assert np.count_nonzero(raster == 0) > 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, vector_datasets, shipping_polygons):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        merged = merge_all_exclusions(vector_datasets, shipping_polygons)

        width, height, transform, crs = create_reference_grid(
            resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)
        raster = rasterize_vector(merged, template, burn_value=1, dtype="uint8")

        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": width,
            "height": height,
            "crs": "EPSG:27700",
            "transform": transform,
        }

        output_path = tmp_path / "offshore_exclusions.tif"
        write_geotiff(raster, profile, str(output_path), band_names=["offshore_exclusions"])

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, raster)

    def test_band_description_set(self, tmp_path, vector_datasets, shipping_polygons):
        """Test that band description is written to GeoTIFF metadata."""
        merged = merge_all_exclusions(vector_datasets, shipping_polygons)

        width, height, transform, crs = create_reference_grid(
            resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)
        raster = rasterize_vector(merged, template, burn_value=1, dtype="uint8")

        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": width,
            "height": height,
            "crs": "EPSG:27700",
            "transform": transform,
        }

        output_path = tmp_path / "offshore_exclusions.tif"
        write_geotiff(raster, profile, str(output_path), band_names=["offshore_exclusions"])

        with rasterio.open(output_path) as src:
            assert src.descriptions[0] == "offshore_exclusions"
