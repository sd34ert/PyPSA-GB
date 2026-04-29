"""
Unit tests for build_land_cover_raster.py

Tests the land cover raster building pipeline:
- Input validation (missing files raise errors)
- Raster reprojection onto canonical GB reference grid
- Nearest-neighbour resampling preserves categorical class codes
- Class code validation (expected LCM 2024 codes 0–21)
- Edge cases (unexpected class codes, all-nodata raster)
- GeoTIFF round-trip (write and read back)
"""

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_land_cover_raster import (
    LCM_VALID_CLASSES,
    reproject_to_reference_grid,
    validate_class_codes,
    validate_inputs,
)
from scripts.utilities.land_utils import write_geotiff

# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_lcm_raster(tmp_path):
    """
    Create a small synthetic LCM raster in EPSG:27700.

    A 210x100 pixel raster at 25m resolution centered within the canonical
    GB bounds. Contains a mix of class codes (1–21) in horizontal bands
    for easy verification.

    Returns the file path to the temporary GeoTIFF.
    """
    width, height = 100, 210
    resolution = 25  # 25m native resolution (LCM 2024 is 25m)
    # Place within canonical GB bounds
    xmin, ymax = 400_000, 306_250
    transform = Affine.translation(xmin, ymax) * Affine.scale(resolution, -resolution)

    # Create class code bands: 10 rows of each class (1–21)
    data = np.zeros((height, width), dtype="uint8")
    for cls in range(1, 22):
        row_start = (cls - 1) * 10
        row_end = cls * 10
        data[row_start:row_end, :] = cls

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 0,
    }

    path = tmp_path / "uk_lcm_2024.tif"
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)

    return str(path)


@pytest.fixture
def single_class_raster(tmp_path):
    """
    Create a raster with a single land cover class (4 = Improved Grassland).

    Tests that reprojection preserves a uniform categorical value.
    """
    width, height = 50, 50
    resolution = 10
    xmin, ymax = 400_000, 301_000
    transform = Affine.translation(xmin, ymax) * Affine.scale(resolution, -resolution)

    data = np.full((height, width), 4, dtype="uint8")

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 0,
    }

    path = tmp_path / "lcm_single_class.tif"
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)

    return str(path)


@pytest.fixture
def invalid_class_raster(tmp_path):
    """
    Create a raster with an invalid class code (255).

    Tests that validate_class_codes catches unexpected values.
    """
    width, height = 50, 50
    resolution = 10
    xmin, ymax = 400_000, 301_000
    transform = Affine.translation(xmin, ymax) * Affine.scale(resolution, -resolution)

    data = np.full((height, width), 255, dtype="uint8")

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 0,
    }

    path = tmp_path / "lcm_invalid.tif"
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)

    return str(path)


@pytest.fixture
def all_nodata_raster(tmp_path):
    """
    Create a raster that is entirely nodata (0).

    Tests edge case where the source raster has no valid land cover data
    within the canonical grid extent.
    """
    width, height = 50, 50
    resolution = 10
    xmin, ymax = 400_000, 301_000
    transform = Affine.translation(xmin, ymax) * Affine.scale(resolution, -resolution)

    data = np.zeros((height, width), dtype="uint8")

    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 0,
    }

    path = tmp_path / "lcm_nodata.tif"
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)

    return str(path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_file_exists(self, small_lcm_raster):
        """Test that validation passes when the LCM file exists."""
        validate_inputs(small_lcm_raster)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing file raises FileNotFoundError."""
        path = str(tmp_path / "nonexistent.tif")

        with pytest.raises(FileNotFoundError, match="LCM raster not found"):
            validate_inputs(path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Reproject to Reference Grid
# ══════════════════════════════════════════════════════════════════════════════


class TestReprojectToReferenceGrid:
    """Test reprojection onto the canonical GB reference grid."""

    def test_returns_array_and_profile(self, small_lcm_raster):
        """Test that function returns (ndarray, dict) tuple."""
        dst_array, dst_profile = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        assert isinstance(dst_array, np.ndarray)
        assert isinstance(dst_profile, dict)

    def test_output_is_2d(self, small_lcm_raster):
        """Test that output raster is single-band (2D)."""
        dst_array, _ = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        assert dst_array.ndim == 2

    def test_output_dtype_uint8(self, small_lcm_raster):
        """Test that output raster uses uint8 dtype."""
        dst_array, dst_profile = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        assert dst_array.dtype == np.uint8
        assert dst_profile["dtype"] == "uint8"

    def test_canonical_grid_dimensions(self, small_lcm_raster):
        """Test that output matches canonical GB grid dimensions at 100m."""
        dst_array, dst_profile = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        # Canonical GB bounds: (0, 0, 700_000, 1_300_000) at 100m
        assert dst_profile["width"] == 7000
        assert dst_profile["height"] == 13000
        assert dst_array.shape == (13000, 7000)

    def test_profile_has_required_keys(self, small_lcm_raster):
        """Test that the rasterio profile has all required keys."""
        _, dst_profile = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        required_keys = {"driver", "dtype", "width", "height", "count", "crs", "transform"}
        assert required_keys.issubset(set(dst_profile.keys()))
        assert dst_profile["count"] == 1
        assert dst_profile["crs"] == "EPSG:27700"

    def test_preserves_class_codes(self, small_lcm_raster):
        """Test that nearest-neighbour resampling preserves categorical class codes."""
        dst_array, _ = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        # Non-zero pixels should only contain valid class codes (1–21)
        nonzero_values = dst_array[dst_array > 0]
        if nonzero_values.size > 0:
            assert nonzero_values.min() >= 1
            assert nonzero_values.max() <= 21

    def test_has_data_pixels(self, small_lcm_raster):
        """Test that output contains at least some non-zero land cover pixels."""
        dst_array, _ = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        data_pixels = np.count_nonzero(dst_array)
        assert data_pixels > 0

    def test_has_nodata_pixels(self, small_lcm_raster):
        """Test that output has nodata pixels (source is much smaller than GB grid)."""
        dst_array, _ = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        nodata_pixels = np.count_nonzero(dst_array == 0)
        assert nodata_pixels > 0

    def test_single_class_preserved(self, single_class_raster):
        """Test that a uniform raster retains its single class code after reproject."""
        dst_array, _ = reproject_to_reference_grid(
            lcm_path=single_class_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        # Non-zero pixels should all be class 4
        nonzero_values = dst_array[dst_array > 0]
        if nonzero_values.size > 0:
            unique_nonzero = np.unique(nonzero_values)
            assert len(unique_nonzero) == 1
            assert unique_nonzero[0] == 4


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Validate Class Codes
# ══════════════════════════════════════════════════════════════════════════════


class TestValidateClassCodes:
    """Test class code validation against expected LCM 2024 codes."""

    def test_valid_codes_pass(self):
        """Test that arrays with only valid codes (0–21) pass validation."""
        data = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]], dtype="uint8")

        # Should not raise
        validate_class_codes(data)

    def test_all_valid_codes_present(self):
        """Test that all 21 class codes plus nodata pass validation."""
        data = np.arange(0, 22, dtype="uint8").reshape(1, -1)

        # Should not raise
        validate_class_codes(data)

    def test_single_nodata_passes(self):
        """Test that an all-nodata array passes validation."""
        data = np.zeros((10, 10), dtype="uint8")

        # Should not raise (0 is valid nodata)
        validate_class_codes(data)

    def test_invalid_code_raises(self):
        """Test that unexpected class codes raise ValueError."""
        data = np.array([[0, 1, 255]], dtype="uint8")

        with pytest.raises(ValueError, match="Unexpected land cover class codes"):
            validate_class_codes(data)

    def test_invalid_code_22_raises(self):
        """Test that class code 22 (above valid range) raises ValueError."""
        data = np.array([[1, 2, 22]], dtype="uint8")

        with pytest.raises(ValueError, match="Unexpected land cover class codes"):
            validate_class_codes(data)

    def test_multiple_invalid_codes_reported(self):
        """Test that all unexpected codes are included in the error message."""
        data = np.array([[22, 50, 200]], dtype="uint8")

        with pytest.raises(ValueError, match="22"):
            validate_class_codes(data)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: LCM Valid Classes Constant
# ══════════════════════════════════════════════════════════════════════════════


class TestLCMValidClasses:
    """Test that the LCM_VALID_CLASSES constant is correctly defined."""

    def test_contains_nodata(self):
        """Test that 0 (nodata) is in valid classes."""
        assert 0 in LCM_VALID_CLASSES

    def test_contains_all_21_classes(self):
        """Test that all 21 habitat classes (1–21) are present."""
        for cls in range(1, 22):
            assert cls in LCM_VALID_CLASSES

    def test_no_extra_classes(self):
        """Test that no codes above 21 are in valid classes."""
        assert LCM_VALID_CLASSES == set(range(0, 22))

    def test_total_count(self):
        """Test that there are exactly 22 valid classes (0–21)."""
        assert len(LCM_VALID_CLASSES) == 22


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, small_lcm_raster):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        dst_array, dst_profile = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        output_path = tmp_path / "land_cover_gb.tif"
        write_geotiff(dst_array, dst_profile, str(output_path), nodata=0)

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, dst_array)

    def test_nodata_value_is_zero(self, tmp_path, small_lcm_raster):
        """Test that the written GeoTIFF has nodata=0."""
        dst_array, dst_profile = reproject_to_reference_grid(
            lcm_path=small_lcm_raster,
            resolution=100,
            target_crs="EPSG:27700",
        )

        output_path = tmp_path / "land_cover_gb.tif"
        write_geotiff(dst_array, dst_profile, str(output_path), nodata=0)

        with rasterio.open(output_path) as src:
            assert src.nodata == 0
