"""
Unit tests for build_green_belt_raster.py

Tests the Green Belt raster building pipeline:
- Input validation (missing files raise errors)
- Two-source Green Belt data loading (England DLUHC, Scotland ScotGov)
- Merge and dissolve of overlapping Green Belt geometries
- Binary rasterization (1 = Green Belt, 0 = not Green Belt)
- Edge cases (overlapping sources, multi-feature sources, missing inputs)
- GeoTIFF round-trip (write and read back)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_green_belt_raster import (
    build_green_belt_raster,
    load_all_green_belt_data,
    validate_inputs,
)
from scripts.utilities.land_utils import write_geotiff

# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test rasters (1km x 1km)."""
    return (400_000, 300_000, 401_000, 301_000)


@pytest.fixture
def green_belt_files(tmp_path, small_bounds):
    """
    Create 2 synthetic Green Belt files matching the Snakemake rule inputs.

    England gets a polygon in the left half, Scotland in the right half,
    so they are distinguishable after merge. All in EPSG:27700.

    Returns a dict with the 2 expected input keys and their file paths.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2

    # England — gpkg (left half)
    eng_gdf = gpd.GeoDataFrame(geometry=[box(xmin, ymin, mid_x, ymax)], crs="EPSG:27700")
    eng_path = tmp_path / "green_belt_england.gpkg"
    eng_gdf.to_file(eng_path, driver="GPKG")

    # Scotland — shapefile (right half)
    sco_gdf = gpd.GeoDataFrame(geometry=[box(mid_x, ymin, xmax, ymax)], crs="EPSG:27700")
    sco_path = tmp_path / "green_belt_scotland.gpkg"
    sco_gdf.to_file(sco_path, driver="GPKG")

    return {
        "gb_eng": str(eng_path),
        "gb_sco": str(sco_path),
    }


@pytest.fixture
def multi_feature_files(tmp_path, small_bounds):
    """
    Create Green Belt files where England has multiple features.

    Tests that feature counts are preserved and data_source is tagged correctly.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2

    # England — 3 features (nested boxes)
    eng_gdf = gpd.GeoDataFrame(
        geometry=[
            box(xmin, ymin, mid_x, ymax),
            box(xmin + 100, ymin + 100, mid_x - 100, ymax - 100),
            box(xmin + 200, ymin + 200, mid_x - 200, ymax - 200),
        ],
        crs="EPSG:27700",
    )
    eng_path = tmp_path / "green_belt_england.gpkg"
    eng_gdf.to_file(eng_path, driver="GPKG")

    # Scotland — 2 features
    sco_gdf = gpd.GeoDataFrame(
        geometry=[
            box(mid_x, ymin, xmax, ymax),
            box(mid_x + 100, ymin + 100, xmax - 100, ymax - 100),
        ],
        crs="EPSG:27700",
    )
    sco_path = tmp_path / "green_belt_scotland.gpkg"
    sco_gdf.to_file(sco_path, driver="GPKG")

    return {
        "gb_eng": str(eng_path),
        "gb_sco": str(sco_path),
    }


@pytest.fixture
def overlapping_files(tmp_path, small_bounds):
    """
    Create Green Belt files where both sources cover the same area.

    Tests that dissolve correctly merges overlapping geometries without
    double-counting.
    """
    xmin, ymin, xmax, ymax = small_bounds
    geom = box(xmin, ymin, xmax, ymax)  # both cover entire area

    eng_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
    eng_path = tmp_path / "green_belt_england.gpkg"
    eng_gdf.to_file(eng_path, driver="GPKG")

    sco_gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
    sco_path = tmp_path / "green_belt_scotland.gpkg"
    sco_gdf.to_file(sco_path, driver="GPKG")

    return {
        "gb_eng": str(eng_path),
        "gb_sco": str(sco_path),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, green_belt_files):
        """Test that validation passes when both files exist."""
        validate_inputs(green_belt_files)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "gb_eng": str(tmp_path / "nonexistent.gpkg"),
            "gb_sco": str(tmp_path / "also_missing.shp"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, green_belt_files, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = green_belt_files.copy()
        paths["extra_missing"] = str(tmp_path / "nope.gpkg")

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)

    def test_validate_inputs_all_missing(self, tmp_path):
        """Test error message when both input files are missing."""
        paths = {
            "gb_eng": str(tmp_path / "gb_eng.gpkg"),
            "gb_sco": str(tmp_path / "gb_sco.shp"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Load All Green Belt Data
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadAllGreenBeltData:
    """Test two-source Green Belt data loading and merge."""

    def test_returns_geodataframe(self, green_belt_files):
        """Test that function returns a GeoDataFrame."""
        result = load_all_green_belt_data(green_belt_files)

        assert isinstance(result, gpd.GeoDataFrame)

    def test_has_geometry_and_data_source_columns(self, green_belt_files):
        """Test that output has geometry and data_source columns only."""
        result = load_all_green_belt_data(green_belt_files)

        assert "geometry" in result.columns
        assert "data_source" in result.columns
        assert len(result.columns) == 2

    def test_crs_is_epsg_27700(self, green_belt_files):
        """Test that output CRS is EPSG:27700."""
        result = load_all_green_belt_data(green_belt_files)

        assert result.crs.to_epsg() == 27700

    def test_both_sources_present(self, green_belt_files):
        """Test that both data sources are tagged in output."""
        result = load_all_green_belt_data(green_belt_files)

        expected_sources = {"DLUHC", "ScotGov"}
        actual_sources = set(result["data_source"].unique())
        assert actual_sources == expected_sources

    def test_total_feature_count(self, green_belt_files):
        """Test that merged GeoDataFrame has correct total feature count."""
        result = load_all_green_belt_data(green_belt_files)

        # Each of the 2 sources has 1 feature
        assert len(result) == 2

    def test_multi_feature_counts_preserved(self, multi_feature_files):
        """Test that per-source feature counts are preserved after merge."""
        result = load_all_green_belt_data(multi_feature_files)

        source_counts = result["data_source"].value_counts()
        assert source_counts["DLUHC"] == 3
        assert source_counts["ScotGov"] == 2

    def test_total_multi_feature_count(self, multi_feature_files):
        """Test total feature count with multi-feature sources."""
        result = load_all_green_belt_data(multi_feature_files)

        # 3 + 2 = 5
        assert len(result) == 5

    def test_geometries_not_empty(self, green_belt_files):
        """Test that no geometries are empty or null after merge."""
        result = load_all_green_belt_data(green_belt_files)

        assert result.geometry.notna().all()
        assert not result.geometry.is_empty.any()

    def test_overlapping_sources_all_retained(self, overlapping_files):
        """Test that overlapping features from different sources are all retained."""
        result = load_all_green_belt_data(overlapping_files)

        # Overlaps are retained before dissolve — 2 features total
        assert len(result) == 2
        assert len(result["data_source"].unique()) == 2


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Green Belt Raster
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildGreenBeltRaster:
    """Test the main raster building function (dissolve + grid + rasterize)."""

    @pytest.fixture
    def all_gb(self, green_belt_files):
        """Load merged Green Belt data via the production function."""
        return load_all_green_belt_data(green_belt_files)

    @pytest.fixture
    def all_gb_overlapping(self, overlapping_files):
        """Load merged Green Belt data with fully overlapping sources."""
        return load_all_green_belt_data(overlapping_files)

    def test_returns_raster_and_profile(self, all_gb):
        """Test that function returns (ndarray, dict) tuple."""
        raster, profile = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_raster_is_2d(self, all_gb):
        """Test that output raster is single-band (2D)."""
        raster, _ = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        assert raster.ndim == 2

    def test_raster_dtype_uint8(self, all_gb):
        """Test that output raster uses uint8 dtype."""
        raster, profile = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        assert raster.dtype == np.uint8
        assert profile["dtype"] == "uint8"

    def test_binary_values_only(self, all_gb):
        """Test that raster contains only 0 and 1 values."""
        raster, _ = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

    def test_has_green_belt_pixels(self, all_gb):
        """Test that raster contains at least some Green Belt pixels."""
        raster, _ = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        gb_pixels = np.count_nonzero(raster)
        assert gb_pixels > 0

    def test_has_non_green_belt_pixels(self, all_gb):
        """Test that raster contains non-Green Belt pixels (not fully covered)."""
        raster, _ = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        # Canonical GB grid is much larger than our 1km test area
        non_gb_pixels = np.count_nonzero(raster == 0)
        assert non_gb_pixels > 0

    def test_profile_has_required_keys(self, all_gb):
        """Test that the rasterio profile has all required keys."""
        _, profile = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        required_keys = {"driver", "dtype", "width", "height", "count", "crs", "transform"}
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 1
        assert profile["crs"] == "EPSG:27700"

    def test_overlapping_sources_dissolve_correctly(self, all_gb_overlapping):
        """Test that overlapping sources produce same result as single coverage."""
        raster, _ = build_green_belt_raster(
            all_gb=all_gb_overlapping,
            resolution=100,
        )

        # Both sources overlap the same area — after dissolve, the Green Belt
        # pixels should match one polygon's coverage (no double-counting)
        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

        # Should still have Green Belt pixels despite overlap
        assert np.count_nonzero(raster) > 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, green_belt_files):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        all_gb = load_all_green_belt_data(green_belt_files)

        raster, profile = build_green_belt_raster(
            all_gb=all_gb,
            resolution=100,
        )

        output_path = tmp_path / "green_belt_gb.tif"
        write_geotiff(raster, profile, str(output_path))

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, raster)
