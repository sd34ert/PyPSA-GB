"""
Unit tests for build_groundwater_protection.py

Tests the groundwater protection zone raster building pipeline:
- Input validation (missing files raise errors)
- E&W SPZ data loading (Defra/EA, NRW) with zone number preservation
- SPZ zone classification (SPZ1, SPZ2, SPZ3, dropping zone 4)
- 3-band rasterization (Band 1 = SPZ1, Band 2 = SPZ2, Band 3 = SPZ3)
- Edge cases (overlapping sources, mixed dtypes, missing inputs)
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

from scripts.land.build_groundwater_protection import (
    build_groundwater_spz_raster,
    classify_spz_zones,
    load_ew_spz_data,
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
def spz_gpkgs(tmp_path, small_bounds):
    """
    Create 2 synthetic SPZ GeoPackages for England and Wales.

    Each source gets features with SPZ zone numbers (1, 2, 3) to test
    zone classification. England uses string dtype, Wales uses int dtype
    to mirror real data.

    Returns a dict with the 2 expected input keys and their file paths.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    # England — string number column (matches real Defra data)
    eng_gdf = gpd.GeoDataFrame(
        {
            "number": ["1", "1c", "2", "2c", "3", "3c", "4"],
            "geometry": [
                box(xmin, mid_y, mid_x, ymax),
                box(xmin + 50, mid_y + 50, mid_x - 50, ymax - 50),
                box(xmin, ymin, mid_x, mid_y),
                box(xmin + 50, ymin + 50, mid_x - 50, mid_y - 50),
                box(mid_x, mid_y, xmax, ymax),
                box(mid_x + 50, mid_y + 50, xmax - 50, ymax - 50),
                box(mid_x, ymin, xmax, mid_y),  # zone 4 — should be dropped
            ],
        },
        crs="EPSG:27700",
    )
    eng_path = tmp_path / "spz_eng.gpkg"
    eng_gdf.to_file(eng_path, driver="GPKG")

    # Wales — integer number column (matches real NRW data)
    wal_gdf = gpd.GeoDataFrame(
        {
            "number": [1, 2, 3],
            "geometry": [
                box(xmin + 200, mid_y + 200, mid_x - 200, ymax - 200),
                box(xmin + 200, ymin + 200, mid_x - 200, mid_y - 200),
                box(mid_x + 200, mid_y + 200, xmax - 200, ymax - 200),
            ],
        },
        crs="EPSG:27700",
    )
    wal_path = tmp_path / "spz_wal.gpkg"
    wal_gdf.to_file(wal_path, driver="GPKG")

    return {
        "spz_eng": str(eng_path),
        "spz_wal": str(wal_path),
    }


@pytest.fixture
def overlapping_spz_gpkgs(tmp_path, small_bounds):
    """
    Create SPZ GPKGs where both sources overlap in the same area.

    Tests that dissolve correctly merges overlapping geometries without
    double-counting.
    """
    xmin, ymin, xmax, ymax = small_bounds
    geom = box(xmin, ymin, xmax, ymax)

    eng_gdf = gpd.GeoDataFrame(
        {"number": ["1"], "geometry": [geom]},
        crs="EPSG:27700",
    )
    eng_path = tmp_path / "spz_eng.gpkg"
    eng_gdf.to_file(eng_path, driver="GPKG")

    wal_gdf = gpd.GeoDataFrame(
        {"number": [1], "geometry": [geom]},
        crs="EPSG:27700",
    )
    wal_path = tmp_path / "spz_wal.gpkg"
    wal_gdf.to_file(wal_path, driver="GPKG")

    return {
        "spz_eng": str(eng_path),
        "spz_wal": str(wal_path),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, spz_gpkgs):
        """Test that validation passes when all files exist."""
        validate_inputs(spz_gpkgs)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "spz_eng": str(tmp_path / "nonexistent.gpkg"),
            "spz_wal": str(tmp_path / "also_missing.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, spz_gpkgs, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = spz_gpkgs.copy()
        paths["extra_missing"] = str(tmp_path / "nope.gpkg")

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Load E&W SPZ Data
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadEwSpzData:
    """Test England & Wales SPZ data loading and merge."""

    def test_returns_geodataframe(self, spz_gpkgs):
        """Test that function returns a GeoDataFrame."""
        result = load_ew_spz_data(spz_gpkgs)
        assert isinstance(result, gpd.GeoDataFrame)

    def test_has_required_columns(self, spz_gpkgs):
        """Test that output has number, geometry, and data_source columns."""
        result = load_ew_spz_data(spz_gpkgs)

        assert "geometry" in result.columns
        assert "data_source" in result.columns
        assert "number" in result.columns

    def test_crs_is_epsg_27700(self, spz_gpkgs):
        """Test that output CRS is EPSG:27700."""
        result = load_ew_spz_data(spz_gpkgs)
        assert result.crs.to_epsg() == 27700

    def test_both_sources_present(self, spz_gpkgs):
        """Test that both data sources are tagged in output."""
        result = load_ew_spz_data(spz_gpkgs)

        expected_sources = {"Defra_EA", "NRW"}
        actual_sources = set(result["data_source"].unique())
        assert actual_sources == expected_sources

    def test_total_feature_count(self, spz_gpkgs):
        """Test that merged GeoDataFrame has correct total feature count."""
        result = load_ew_spz_data(spz_gpkgs)
        # England has 7, Wales has 3
        assert len(result) == 10

    def test_number_column_preserved(self, spz_gpkgs):
        """Test that number column values are preserved after merge."""
        result = load_ew_spz_data(spz_gpkgs)
        assert result["number"].notna().all()

    def test_geometries_not_empty(self, spz_gpkgs):
        """Test that no geometries are empty or null after merge."""
        result = load_ew_spz_data(spz_gpkgs)

        assert result.geometry.notna().all()
        assert not result.geometry.is_empty.any()


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Classify SPZ Zones
# ══════════════════════════════════════════════════════════════════════════════


class TestClassifySpzZones:
    """Test SPZ zone classification into Bands 1, 2, and 3."""

    @pytest.fixture
    def merged_spz(self, spz_gpkgs):
        """Load merged SPZ data via the production function."""
        return load_ew_spz_data(spz_gpkgs)

    def test_returns_three_geodataframes(self, merged_spz):
        """Test that function returns a tuple of three GeoDataFrames."""
        spz1, spz2, spz3 = classify_spz_zones(merged_spz)

        assert isinstance(spz1, gpd.GeoDataFrame)
        assert isinstance(spz2, gpd.GeoDataFrame)
        assert isinstance(spz3, gpd.GeoDataFrame)

    def test_spz1_captures_zone_1_and_1c(self, merged_spz):
        """Test that Band 1 captures '1' and '1c' (and int 1)."""
        spz1, _, _ = classify_spz_zones(merged_spz)

        # England: '1', '1c' = 2 features; Wales: 1 = 1 feature
        assert len(spz1) == 3

    def test_spz2_captures_zone_2_and_2c(self, merged_spz):
        """Test that Band 2 captures '2' and '2c' (and int 2)."""
        _, spz2, _ = classify_spz_zones(merged_spz)

        # England: '2', '2c' = 2 features; Wales: 2 = 1 feature
        assert len(spz2) == 3

    def test_spz3_captures_zone_3_and_3c(self, merged_spz):
        """Test that Band 3 captures '3' and '3c' (and int 3)."""
        _, _, spz3 = classify_spz_zones(merged_spz)

        # England: '3', '3c' = 2 features; Wales: 3 = 1 feature
        assert len(spz3) == 3

    def test_zone_4_dropped(self, merged_spz):
        """Test that zone 4 features are dropped."""
        spz1, spz2, spz3 = classify_spz_zones(merged_spz)

        total_classified = len(spz1) + len(spz2) + len(spz3)
        # 10 total - 1 zone 4 = 9 classified
        assert total_classified == 9

    def test_handles_mixed_dtypes(self):
        """Test classification handles mixed string/int number column."""
        gdf = gpd.GeoDataFrame(
            {
                "number": ["1", "1c", 2, "3c", 4],
                "geometry": [box(0, 0, 1, 1)] * 5,
                "data_source": ["A"] * 5,
            },
            crs="EPSG:27700",
        )

        spz1, spz2, spz3 = classify_spz_zones(gdf)

        assert len(spz1) == 2  # '1', '1c'
        assert len(spz2) == 1  # 2 (as '2')
        assert len(spz3) == 1  # '3c'


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Groundwater SPZ Raster (3-band)
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildDwpSpzRaster:
    """Test the main 3-band raster building function."""

    @pytest.fixture
    def all_spz(self, spz_gpkgs):
        """Load merged SPZ data via the production function."""
        return load_ew_spz_data(spz_gpkgs)

    @pytest.fixture
    def all_spz_overlapping(self, overlapping_spz_gpkgs):
        """Load merged SPZ data with fully overlapping sources."""
        return load_ew_spz_data(overlapping_spz_gpkgs)

    def test_returns_raster_and_profile(self, all_spz):
        """Test that function returns (ndarray, dict) tuple."""
        raster, profile = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_raster_is_3d_with_3_bands(self, all_spz):
        """Test that output raster has shape (3, height, width)."""
        raster, _ = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        assert raster.ndim == 3
        assert raster.shape[0] == 3

    def test_raster_dtype_uint8(self, all_spz):
        """Test that output raster uses uint8 dtype."""
        raster, profile = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        assert raster.dtype == np.uint8
        assert profile["dtype"] == "uint8"

    def test_binary_values_only(self, all_spz):
        """Test that all bands contain only 0 and 1 values."""
        raster, _ = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        for band_idx in range(3):
            unique_values = np.unique(raster[band_idx])
            assert set(unique_values).issubset({0, 1})

    def test_band1_has_spz_pixels(self, all_spz):
        """Test that Band 1 (SPZ1) has some protected pixels."""
        raster, _ = build_groundwater_spz_raster(
            all_spz, resolution=100
        )
        assert np.count_nonzero(raster[0]) > 0

    def test_band2_has_spz_pixels(self, all_spz):
        """Test that Band 2 (SPZ2) has some protected pixels."""
        raster, _ = build_groundwater_spz_raster(
            all_spz, resolution=100
        )
        assert np.count_nonzero(raster[1]) > 0

    def test_band3_has_spz_pixels(self, all_spz):
        """Test that Band 3 (SPZ3) has some protected pixels."""
        raster, _ = build_groundwater_spz_raster(
            all_spz, resolution=100
        )
        assert np.count_nonzero(raster[2]) > 0

    def test_profile_has_required_keys(self, all_spz):
        """Test that the rasterio profile has all required keys."""
        _, profile = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        required_keys = {
            "driver", "dtype", "width", "height",
            "count", "crs", "transform",
        }
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 3
        assert profile["crs"] == "EPSG:27700"

    def test_overlapping_sources_dissolve_correctly(
        self, all_spz_overlapping
    ):
        """Test that overlapping sources produce binary values."""
        raster, _ = build_groundwater_spz_raster(
            all_spz_overlapping, resolution=100
        )

        for band_idx in range(3):
            unique_values = np.unique(raster[band_idx])
            assert set(unique_values).issubset({0, 1})


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, spz_gpkgs):
        """Test that the output GeoTIFF is valid and readable."""
        all_spz = load_ew_spz_data(spz_gpkgs)
        raster, profile = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        output_path = tmp_path / "groundwater_spz.tif"
        band_names = ["SPZ1", "SPZ2", "SPZ3"]
        write_geotiff(
            raster, profile, str(output_path),
            band_names=band_names,
        )

        with rasterio.open(output_path) as src:
            assert src.count == 3
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            for i in range(3):
                band = src.read(i + 1)
                np.testing.assert_array_equal(
                    band, raster[i]
                )

    def test_band_descriptions(self, tmp_path, spz_gpkgs):
        """Test that band descriptions are written correctly."""
        all_spz = load_ew_spz_data(spz_gpkgs)
        raster, profile = build_groundwater_spz_raster(
            all_spz, resolution=100
        )

        output_path = tmp_path / "groundwater_spz.tif"
        band_names = ["SPZ1", "SPZ2", "SPZ3"]
        write_geotiff(
            raster, profile, str(output_path),
            band_names=band_names,
        )

        with rasterio.open(output_path) as src:
            assert src.descriptions[0] == "SPZ1"
            assert src.descriptions[1] == "SPZ2"
            assert src.descriptions[2] == "SPZ3"
