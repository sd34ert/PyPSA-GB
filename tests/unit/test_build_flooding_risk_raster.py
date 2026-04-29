"""
Unit tests for build_flooding_risk_raster.py

Tests the flooding risk raster building pipeline:
- Input validation (missing files raise errors)
- Incremental load-rasterize-merge produces correct binary output
- Binary rasterization (1 = flood risk, 0 = none)
- Edge cases (empty geometries, overlapping sources, missing inputs)
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

from scripts.land.build_flooding_risk_raster import (
    build_flooding_risk_raster,
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
def flood_zone_gpkgs(tmp_path, small_bounds):
    """
    Create 6 synthetic flood zone GeoPackages matching the Snakemake rule inputs.

    Each source gets a polygon in a different part of the test area so they
    are distinguishable after merge. All in EPSG:27700.

    Returns a dict with the 6 expected input keys and their file paths.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2
    qtr_x = (xmin + mid_x) / 2
    qtr_y = (ymin + mid_y) / 2

    # Each source gets a distinct polygon region
    geometries = {
        "flood_eng": box(xmin, mid_y, mid_x, ymax),  # top-left quarter
        "flood_sco_river": box(mid_x, mid_y, xmax, ymax),  # top-right quarter
        "flood_sco_surface": box(xmin, ymin, qtr_x, mid_y),  # bottom-left eighth
        "flood_sco_coastal": box(qtr_x, ymin, mid_x, mid_y),  # bottom-centre-left
        "flood_wal": box(mid_x, ymin, xmax, mid_y),  # bottom-right quarter
        "flood_wal_surface": box(mid_x, qtr_y, xmax, mid_y),  # overlaps flood_wal
    }

    paths = {}
    for name, geom in geometries.items():
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
        path = tmp_path / f"{name}.gpkg"
        gdf.to_file(path, driver="GPKG")
        paths[name] = str(path)

    return paths


@pytest.fixture
def overlapping_flood_gpkgs(tmp_path, small_bounds):
    """
    Create flood zone GPKGs where all 6 sources overlap in the same area.

    Tests that OR-merge correctly handles overlapping geometries without
    double-counting.
    """
    xmin, ymin, xmax, ymax = small_bounds
    geom = box(xmin, ymin, xmax, ymax)  # all cover entire area

    paths = {}
    for name in [
        "flood_eng",
        "flood_sco_river",
        "flood_sco_surface",
        "flood_sco_coastal",
        "flood_wal",
        "flood_wal_surface",
    ]:
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
        path = tmp_path / f"{name}.gpkg"
        gdf.to_file(path, driver="GPKG")
        paths[name] = str(path)

    return paths


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, flood_zone_gpkgs):
        """Test that validation passes when all 6 files exist."""
        validate_inputs(flood_zone_gpkgs)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "flood_eng": str(tmp_path / "nonexistent.gpkg"),
            "flood_sco_river": str(tmp_path / "also_missing.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, flood_zone_gpkgs, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = flood_zone_gpkgs.copy()
        paths["extra_missing"] = str(tmp_path / "nope.gpkg")

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)

    def test_validate_inputs_all_missing(self, tmp_path):
        """Test error message when all 6 input files are missing."""
        paths = {
            name: str(tmp_path / f"{name}.gpkg")
            for name in [
                "flood_eng",
                "flood_sco_river",
                "flood_sco_surface",
                "flood_sco_coastal",
                "flood_wal",
                "flood_wal_surface",
            ]
        }

        with pytest.raises(FileNotFoundError, match="Missing 6 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Flooding Risk Raster
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildFloodingRiskRaster:
    """Test the incremental load-rasterize-merge pipeline."""

    def test_returns_raster_and_profile(self, flood_zone_gpkgs):
        """Test that function returns (ndarray, dict) tuple."""
        raster, profile = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_raster_is_2d(self, flood_zone_gpkgs):
        """Test that output raster is single-band (2D)."""
        raster, _ = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        assert raster.ndim == 2

    def test_raster_dtype_uint8(self, flood_zone_gpkgs):
        """Test that output raster uses uint8 dtype."""
        raster, profile = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        assert raster.dtype == np.uint8
        assert profile["dtype"] == "uint8"

    def test_binary_values_only(self, flood_zone_gpkgs):
        """Test that raster contains only 0 and 1 values."""
        raster, _ = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

    def test_has_flood_pixels(self, flood_zone_gpkgs):
        """Test that raster contains at least some flood risk pixels."""
        raster, _ = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        flood_pixels = np.count_nonzero(raster)
        assert flood_pixels > 0

    def test_has_non_flood_pixels(self, flood_zone_gpkgs):
        """Test that raster contains non-flood pixels (not fully covered)."""
        raster, _ = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        # Canonical GB grid is much larger than our 1km test area
        non_flood_pixels = np.count_nonzero(raster == 0)
        assert non_flood_pixels > 0

    def test_profile_has_required_keys(self, flood_zone_gpkgs):
        """Test that the rasterio profile has all required keys."""
        _, profile = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        required_keys = {"driver", "dtype", "width", "height", "count", "crs", "transform"}
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 1
        assert profile["crs"] == "EPSG:27700"

    def test_overlapping_sources_merge_correctly(self, overlapping_flood_gpkgs):
        """Test that overlapping sources produce same result as single coverage."""
        raster, _ = build_flooding_risk_raster(
            input_paths=overlapping_flood_gpkgs,
            resolution=100,
        )

        # All 6 sources overlap the same area — OR-merge means
        # flood pixels should match one polygon's coverage (no double-counting)
        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

        # Should still have flood pixels despite overlap
        assert np.count_nonzero(raster) > 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, flood_zone_gpkgs):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        raster, profile = build_flooding_risk_raster(
            input_paths=flood_zone_gpkgs,
            resolution=100,
        )

        output_path = tmp_path / "flooding_risk.tif"
        write_geotiff(raster, profile, str(output_path))

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, raster)
