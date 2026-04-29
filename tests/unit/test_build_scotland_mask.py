"""
Unit tests for build_scotland_mask.py

Tests the Scotland mask raster building pipeline:
- Zone loading and Scottish zone filtering
- Binary rasterization (1 = Scotland, 0 = not Scotland)
- Scottish zones CSV output
- Edge cases (no match, partial match, all-Scotland input)
- GeoTIFF round-trip (write and read back)
- Error handling (invalid zone names, missing files)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from shapely.geometry import box

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_scotland_mask import (
    build_scotland_mask,
    load_and_filter_scottish_zones,
    write_scotland_zones_csv,
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
def zones_file(tmp_path, small_bounds):
    """
    Create a synthetic zones file with 5 zones mimicking the zonal network.

    3 Scottish zones (Z1_1, Z1_2, Z4) and 2 non-Scottish zones (Z7, Z8).
    All in EPSG:27700 with a 'Name_1' column (matching real zones.geojson).
    """
    xmin, ymin, xmax, ymax = small_bounds
    x_step = (xmax - xmin) / 5

    zone_names = ["Z1_1", "Z1_2", "Z4", "Z7", "Z8"]
    geometries = [box(xmin + i * x_step, ymin, xmin + (i + 1) * x_step, ymax) for i in range(5)]

    gdf = gpd.GeoDataFrame(
        {"Name_1": zone_names, "geometry": geometries},
        crs="EPSG:27700",
    )
    zones_path = tmp_path / "zones.geojson"
    gdf.to_file(zones_path, driver="GeoJSON")

    return str(zones_path)


@pytest.fixture
def zones_file_4326(tmp_path, small_bounds):
    """
    Create zones file in EPSG:4326 to test reprojection.

    Same structure as zones_file but in WGS84, matching real zones.geojson CRS.
    """
    xmin, ymin, xmax, ymax = small_bounds
    x_step = (xmax - xmin) / 5

    zone_names = ["Z1_1", "Z1_2", "Z4", "Z7", "Z8"]
    geometries = [box(xmin + i * x_step, ymin, xmin + (i + 1) * x_step, ymax) for i in range(5)]

    gdf = gpd.GeoDataFrame(
        {"Name_1": zone_names, "geometry": geometries},
        crs="EPSG:27700",
    )
    # Reproject to WGS84 to test that load_zone_shapes reprojects back
    gdf = gdf.to_crs("EPSG:4326")

    zones_path = tmp_path / "zones_4326.geojson"
    gdf.to_file(zones_path, driver="GeoJSON")

    return str(zones_path)


@pytest.fixture
def scottish_zone_names():
    """Default Scottish zone names matching config/defaults.yaml."""
    return ["Z1_1", "Z1_2", "Z4"]


@pytest.fixture
def all_scottish_zone_names():
    """All zones are Scottish — tests full coverage case."""
    return ["Z1_1", "Z1_2", "Z4", "Z7", "Z8"]


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Load and Filter Scottish Zones
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadAndFilterScottishZones:
    """Test zone loading, CRS validation, and Scottish zone filtering."""

    def test_returns_two_geodataframes(self, zones_file, scottish_zone_names):
        """Test that function returns (all_zones, scottish_zones) tuple."""
        all_zones, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        assert isinstance(all_zones, gpd.GeoDataFrame)
        assert isinstance(scottish_zones, gpd.GeoDataFrame)

    def test_all_zones_count(self, zones_file, scottish_zone_names):
        """Test that all 5 zones are loaded."""
        all_zones, _ = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        assert len(all_zones) == 5

    def test_scottish_zones_count(self, zones_file, scottish_zone_names):
        """Test that only Scottish zones are returned."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        assert len(scottish_zones) == 3

    def test_scottish_zone_names_match(self, zones_file, scottish_zone_names):
        """Test that returned Scottish zone names match the config list."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        matched = set(scottish_zones["zone_name"].tolist())
        expected = set(scottish_zone_names)
        assert matched == expected

    def test_crs_is_epsg_27700(self, zones_file, scottish_zone_names):
        """Test that output CRS is EPSG:27700."""
        all_zones, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        assert all_zones.crs.to_epsg() == 27700
        assert scottish_zones.crs.to_epsg() == 27700

    def test_reprojection_from_4326(self, zones_file_4326, scottish_zone_names):
        """Test that zones in EPSG:4326 are reprojected to EPSG:27700."""
        all_zones, scottish_zones = load_and_filter_scottish_zones(
            zones_file_4326, scottish_zone_names
        )

        assert all_zones.crs.to_epsg() == 27700
        assert scottish_zones.crs.to_epsg() == 27700
        assert len(scottish_zones) == 3

    def test_has_zone_name_column(self, zones_file, scottish_zone_names):
        """Test that Name_1 column is renamed to zone_name."""
        all_zones, _ = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        assert "zone_name" in all_zones.columns

    def test_no_match_raises_error(self, zones_file):
        """Test that ValueError is raised when no zones match config."""
        with pytest.raises(ValueError, match="No Scottish zones found"):
            load_and_filter_scottish_zones(zones_file, ["NONEXISTENT_1", "NONEXISTENT_2"])

    def test_partial_match_still_returns_matched(self, zones_file):
        """Test that partial match returns only the matched zones."""
        partial_names = ["Z1_1", "Z1_2", "NONEXISTENT"]

        _, scottish_zones = load_and_filter_scottish_zones(zones_file, partial_names)

        # Should still return the 2 matched zones
        assert len(scottish_zones) == 2
        assert set(scottish_zones["zone_name"].tolist()) == {"Z1_1", "Z1_2"}

    def test_all_zones_scottish(self, zones_file, all_scottish_zone_names):
        """Test that all zones can be classified as Scottish."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, all_scottish_zone_names)

        assert len(scottish_zones) == 5

    def test_missing_file_raises(self, tmp_path, scottish_zone_names):
        """Test that missing zones file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_and_filter_scottish_zones(
                str(tmp_path / "nonexistent.geojson"), scottish_zone_names
            )

    def test_geometries_not_empty(self, zones_file, scottish_zone_names):
        """Test that Scottish zone geometries are valid (not empty or null)."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        assert scottish_zones.geometry.notna().all()
        assert not scottish_zones.geometry.is_empty.any()


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Scotland Mask Raster
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildScotlandMask:
    """Test the main raster building function (grid + rasterize)."""

    @pytest.fixture
    def scottish_zones_gdf(self, zones_file, scottish_zone_names):
        """Load Scottish zones via the production function."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)
        return scottish_zones

    def test_returns_raster_and_profile(self, scottish_zones_gdf):
        """Test that function returns (ndarray, dict) tuple."""
        raster, profile = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_raster_is_2d(self, scottish_zones_gdf):
        """Test that output raster is single-band (2D)."""
        raster, _ = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        assert raster.ndim == 2

    def test_raster_dtype_uint8(self, scottish_zones_gdf):
        """Test that output raster uses uint8 dtype."""
        raster, profile = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        assert raster.dtype == np.uint8
        assert profile["dtype"] == "uint8"

    def test_binary_values_only(self, scottish_zones_gdf):
        """Test that raster contains only 0 and 1 values."""
        raster, _ = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

    def test_has_scotland_pixels(self, scottish_zones_gdf):
        """Test that raster contains at least some Scotland pixels."""
        raster, _ = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        sco_pixels = np.count_nonzero(raster)
        assert sco_pixels > 0

    def test_has_non_scotland_pixels(self, scottish_zones_gdf):
        """Test that raster contains non-Scotland pixels (not fully covered)."""
        raster, _ = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        # Canonical GB grid is much larger than our 1km test area
        non_sco_pixels = np.count_nonzero(raster == 0)
        assert non_sco_pixels > 0

    def test_profile_has_required_keys(self, scottish_zones_gdf):
        """Test that the rasterio profile has all required keys."""
        _, profile = build_scotland_mask(
            scottish_zones=scottish_zones_gdf,
            resolution=100,
        )

        required_keys = {"driver", "dtype", "width", "height", "count", "crs", "transform"}
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 1
        assert profile["crs"] == "EPSG:27700"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Write Scotland Zones CSV
# ══════════════════════════════════════════════════════════════════════════════


class TestWriteScotlandZonesCSV:
    """Test CSV output of Scottish zone identifiers."""

    @pytest.fixture
    def scottish_zones_gdf(self, zones_file, scottish_zone_names):
        """Load Scottish zones via the production function."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)
        return scottish_zones

    def test_csv_file_created(self, tmp_path, scottish_zones_gdf):
        """Test that CSV file is created at the expected path."""
        output_path = tmp_path / "scotland_zones.csv"
        write_scotland_zones_csv(scottish_zones_gdf, str(output_path))

        assert output_path.exists()

    def test_csv_has_zone_column(self, tmp_path, scottish_zones_gdf):
        """Test that CSV has a 'zone' column."""
        output_path = tmp_path / "scotland_zones.csv"
        write_scotland_zones_csv(scottish_zones_gdf, str(output_path))

        df = pd.read_csv(output_path)
        assert "zone" in df.columns
        assert len(df.columns) == 1

    def test_csv_zone_count(self, tmp_path, scottish_zones_gdf):
        """Test that CSV contains correct number of Scottish zones."""
        output_path = tmp_path / "scotland_zones.csv"
        write_scotland_zones_csv(scottish_zones_gdf, str(output_path))

        df = pd.read_csv(output_path)
        assert len(df) == 3

    def test_csv_zone_names_match(self, tmp_path, scottish_zones_gdf, scottish_zone_names):
        """Test that CSV zone names match the expected Scottish zones."""
        output_path = tmp_path / "scotland_zones.csv"
        write_scotland_zones_csv(scottish_zones_gdf, str(output_path))

        df = pd.read_csv(output_path)
        assert set(df["zone"].tolist()) == set(scottish_zone_names)

    def test_csv_zones_sorted(self, tmp_path, scottish_zones_gdf):
        """Test that CSV zone names are sorted alphabetically."""
        output_path = tmp_path / "scotland_zones.csv"
        write_scotland_zones_csv(scottish_zones_gdf, str(output_path))

        df = pd.read_csv(output_path)
        zone_list = df["zone"].tolist()
        assert zone_list == sorted(zone_list)

    def test_csv_creates_parent_dirs(self, tmp_path, scottish_zones_gdf):
        """Test that CSV output creates parent directories if needed."""
        output_path = tmp_path / "nested" / "dir" / "scotland_zones.csv"
        write_scotland_zones_csv(scottish_zones_gdf, str(output_path))

        assert output_path.exists()


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, zones_file, scottish_zone_names):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        _, scottish_zones = load_and_filter_scottish_zones(zones_file, scottish_zone_names)

        raster, profile = build_scotland_mask(
            scottish_zones=scottish_zones,
            resolution=100,
        )

        output_path = tmp_path / "scotland_mask.tif"
        write_geotiff(raster, profile, str(output_path), band_names=["scotland_mask"])

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"
            assert src.descriptions[0] == "scotland_mask"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, raster)
