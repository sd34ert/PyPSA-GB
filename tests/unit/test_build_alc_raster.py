"""
Unit tests for build_alc_raster.py

Tests the ALC BMV raster building pipeline with pre-filtered inputs:
- Input validation (missing files raise errors)
- Loading and merging pre-filtered BMV GeoPackages
- Dissolve of overlapping BMV geometries
- Binary rasterization (1 = BMV, 0 = not BMV)
- Edge cases (overlapping sources, empty inputs)
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

from scripts.land.build_alc_raster import (
    build_alc_raster,
    load_all_bmv_data,
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
def england_bmv_gdf(small_bounds):
    """
    Create synthetic England BMV GeoDataFrame.

    Mimics alc_bmv_england.gpkg with pre-filtered BMV features.
    """
    xmin, ymin, xmax, ymax = small_bounds
    step = (xmax - xmin) / 3

    return gpd.GeoDataFrame(
        {
            "ALC_GRADE": ["Grade 1", "Grade 2", "Grade 3a"],
            "geometry": [
                box(xmin + i * step, ymin, xmin + (i + 1) * step, ymax)
                for i in range(3)
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def wales_bmv_gdf(small_bounds):
    """
    Create synthetic Wales BMV GeoDataFrame.

    Mimics alc_bmv_wales.gpkg with pre-filtered BMV features.
    """
    xmin, ymin, xmax, ymax = small_bounds
    step = (xmax - xmin) / 3

    return gpd.GeoDataFrame(
        {
            "predictive": ["1", "2", "3a"],
            "geometry": [
                box(xmin + i * step, ymin, xmin + (i + 1) * step, ymax)
                for i in range(3)
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def scotland_bmv_gdf(small_bounds):
    """
    Create synthetic Scotland BMV GeoDataFrame.

    Mimics alc_bmv_scotland.gpkg with pre-filtered BMV features.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2

    return gpd.GeoDataFrame(
        {
            "LCCODE": [1.0, 2.0],
            "geometry": [
                box(xmin, ymin, mid_x, ymax),
                box(mid_x, ymin, xmax, ymax),
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def bmv_files(tmp_path, england_bmv_gdf, wales_bmv_gdf, scotland_bmv_gdf):
    """
    Write synthetic BMV data to files matching Snakemake rule inputs.

    Returns a dict with the 3 expected input keys and their file paths.
    """
    eng_path = tmp_path / "alc_bmv_england.gpkg"
    england_bmv_gdf.to_file(eng_path, driver="GPKG")

    wal_path = tmp_path / "alc_bmv_wales.gpkg"
    wales_bmv_gdf.to_file(wal_path, driver="GPKG")

    sco_path = tmp_path / "alc_bmv_scotland.gpkg"
    scotland_bmv_gdf.to_file(sco_path, driver="GPKG")

    return {
        "alc_eng": str(eng_path),
        "alc_wal": str(wal_path),
        "alc_sco": str(sco_path),
    }


@pytest.fixture
def overlapping_bmv_files(tmp_path, small_bounds):
    """
    Create BMV files where all three nations cover the same area.

    Tests that dissolve correctly merges overlapping geometries.
    """
    xmin, ymin, xmax, ymax = small_bounds
    geom = box(xmin, ymin, xmax, ymax)

    eng_gdf = gpd.GeoDataFrame(
        {"ALC_GRADE": ["Grade 1"], "geometry": [geom]},
        crs="EPSG:27700",
    )
    eng_path = tmp_path / "alc_bmv_england.gpkg"
    eng_gdf.to_file(eng_path, driver="GPKG")

    wal_gdf = gpd.GeoDataFrame(
        {"predictive": ["1"], "geometry": [geom]},
        crs="EPSG:27700",
    )
    wal_path = tmp_path / "alc_bmv_wales.gpkg"
    wal_gdf.to_file(wal_path, driver="GPKG")

    sco_gdf = gpd.GeoDataFrame(
        {"LCCODE": [1.0], "geometry": [geom]},
        crs="EPSG:27700",
    )
    sco_path = tmp_path / "alc_bmv_scotland.gpkg"
    sco_gdf.to_file(sco_path, driver="GPKG")

    return {
        "alc_eng": str(eng_path),
        "alc_wal": str(wal_path),
        "alc_sco": str(sco_path),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, bmv_files):
        """Test that validation passes when all 3 files exist."""
        validate_inputs(bmv_files)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "alc_eng": str(tmp_path / "nonexistent.gpkg"),
            "alc_wal": str(tmp_path / "also_missing.gpkg"),
            "alc_sco": str(tmp_path / "nope.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 3 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, bmv_files, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = bmv_files.copy()
        paths["alc_sco"] = str(tmp_path / "nope.gpkg")

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Load All BMV Data
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadAllBMVData:
    """Test pre-filtered BMV data loading and merge."""

    def test_returns_geodataframe(self, bmv_files):
        """Test that function returns a GeoDataFrame."""
        result = load_all_bmv_data(bmv_files)

        assert isinstance(result, gpd.GeoDataFrame)

    def test_has_geometry_and_data_source_columns(self, bmv_files):
        """Test that output has geometry and data_source columns only."""
        result = load_all_bmv_data(bmv_files)

        assert "geometry" in result.columns
        assert "data_source" in result.columns
        assert len(result.columns) == 2

    def test_crs_is_epsg_27700(self, bmv_files):
        """Test that output CRS is EPSG:27700."""
        result = load_all_bmv_data(bmv_files)

        assert result.crs.to_epsg() == 27700

    def test_all_three_sources_present(self, bmv_files):
        """Test that all three data sources are tagged in output."""
        result = load_all_bmv_data(bmv_files)

        expected_sources = {"NaturalEngland", "WelshGov", "ScotGov"}
        actual_sources = set(result["data_source"].unique())
        assert actual_sources == expected_sources

    def test_total_bmv_feature_count(self, bmv_files):
        """Test that merged GeoDataFrame has correct total feature count."""
        result = load_all_bmv_data(bmv_files)

        # England: 3 + Wales: 3 + Scotland: 2
        assert len(result) == 8

    def test_per_source_bmv_counts(self, bmv_files):
        """Test that per-source feature counts are correct."""
        result = load_all_bmv_data(bmv_files)

        source_counts = result["data_source"].value_counts()
        assert source_counts["NaturalEngland"] == 3
        assert source_counts["WelshGov"] == 3
        assert source_counts["ScotGov"] == 2

    def test_geometries_not_empty(self, bmv_files):
        """Test that no geometries are empty or null after merge."""
        result = load_all_bmv_data(bmv_files)

        assert result.geometry.notna().all()
        assert not result.geometry.is_empty.any()

    def test_overlapping_sources_all_retained(
        self, overlapping_bmv_files
    ):
        """Test that overlapping features from different sources are retained."""
        result = load_all_bmv_data(overlapping_bmv_files)

        # 3 overlapping BMV features (1 per nation), all retained before dissolve
        assert len(result) == 3
        assert len(result["data_source"].unique()) == 3


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build ALC Raster
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildALCRaster:
    """Test the main raster building function (dissolve + grid + rasterize)."""

    @pytest.fixture
    def all_bmv(self, bmv_files):
        """Load merged BMV data via the production function."""
        return load_all_bmv_data(bmv_files)

    @pytest.fixture
    def all_bmv_overlapping(self, overlapping_bmv_files):
        """Load merged BMV data with fully overlapping sources."""
        return load_all_bmv_data(overlapping_bmv_files)

    def test_returns_raster_and_profile(self, all_bmv):
        """Test that function returns (ndarray, dict) tuple."""
        raster, profile = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_raster_is_2d(self, all_bmv):
        """Test that output raster is single-band (2D)."""
        raster, _ = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        assert raster.ndim == 2

    def test_raster_dtype_uint8(self, all_bmv):
        """Test that output raster uses uint8 dtype."""
        raster, profile = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        assert raster.dtype == np.uint8
        assert profile["dtype"] == "uint8"

    def test_binary_values_only(self, all_bmv):
        """Test that raster contains only 0 and 1 values."""
        raster, _ = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

    def test_has_bmv_pixels(self, all_bmv):
        """Test that raster contains at least some BMV pixels."""
        raster, _ = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        bmv_pixels = np.count_nonzero(raster)
        assert bmv_pixels > 0

    def test_has_non_bmv_pixels(self, all_bmv):
        """Test that raster contains non-BMV pixels (not fully covered)."""
        raster, _ = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        non_bmv_pixels = np.count_nonzero(raster == 0)
        assert non_bmv_pixels > 0

    def test_profile_has_required_keys(self, all_bmv):
        """Test that the rasterio profile has all required keys."""
        _, profile = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        required_keys = {
            "driver", "dtype", "width", "height",
            "count", "crs", "transform",
        }
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 1
        assert profile["crs"] == "EPSG:27700"

    def test_overlapping_sources_dissolve_correctly(
        self, all_bmv_overlapping
    ):
        """Test that overlapping sources produce valid result after dissolve."""
        raster, _ = build_alc_raster(
            all_bmv=all_bmv_overlapping,
            resolution=100,
        )

        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})
        assert np.count_nonzero(raster) > 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, bmv_files):
        """Test that the output GeoTIFF is valid and readable."""
        all_bmv = load_all_bmv_data(bmv_files)

        raster, profile = build_alc_raster(
            all_bmv=all_bmv,
            resolution=100,
        )

        output_path = tmp_path / "alc_bmv_gb.tif"
        write_geotiff(raster, profile, str(output_path))

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, raster)
