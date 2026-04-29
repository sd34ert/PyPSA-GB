"""
Unit tests for build_airfield_raster.py — FRZ circle exclusion pipeline.

Tests the FRZ-based pipeline:
- Input validation (file existence)
- Loading FRZ CSV data and creating Point geometries
- Buffering points to FRZ exclusion circles
- Rasterization to 2-band GeoTIFF (MoD, Civilian)
- GeoTIFF round-trip (write and read back)
"""

import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from shapely.geometry import Point

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_airfield_raster import (
    POLYGON_BAND_NAMES,
    buffer_frz_to_circles,
    load_frz_data,
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
    """Small bounding box for fast test rasters (10km x 10km)."""
    return (400_000, 300_000, 410_000, 310_000)


@pytest.fixture
def civil_csv(tmp_path):
    """Create a synthetic civil FRZ CSV file."""
    data = {
        "frz_id": ["EGR1U001A", "EGR1U002A", "EGR1U003A"],
        "aerodrome_name": ["TESTFIELD", "BIGTOWN", "SMALLVILLE"],
        "operator_type": ["civil", "civil", "civil"],
        "lat_dd": [51.5, 52.0, 51.0],
        "lon_dd": [-1.0, -0.5, -1.5],
        "frz_radius_km": [3.704, 4.63, 3.704],
    }
    path = tmp_path / "civil_aerodromes_frz.csv"
    pd.DataFrame(data).to_csv(path, index=False)
    return str(path)


@pytest.fixture
def mod_csv(tmp_path):
    """Create a synthetic MoD FRZ CSV file."""
    data = {
        "frz_id": ["EGR2U001A", "EGR2U002A"],
        "aerodrome_name": ["RAF TESTBASE", "RAF OTHERBASE"],
        "operator_type": ["military", "military"],
        "lat_dd": [53.0, 54.0],
        "lon_dd": [-2.0, -1.0],
        "frz_radius_km": [3.704, 4.63],
    }
    path = tmp_path / "mod_aerodromes_frz.csv"
    pd.DataFrame(data).to_csv(path, index=False)
    return str(path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Constants
# ══════════════════════════════════════════════════════════════════════════════


class TestConstants:
    """Test that band name constants are correctly defined."""

    def test_polygon_band_names_count(self):
        """Test there are exactly 2 polygon band names."""
        assert len(POLYGON_BAND_NAMES) == 2

    def test_polygon_band_names_values(self):
        """Test band names match expected values."""
        assert POLYGON_BAND_NAMES == ["MoD_Military", "Civilian"]


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, civil_csv, mod_csv):
        """Test that validation passes when both files exist."""
        validate_inputs({"civil_frz": civil_csv, "mod_frz": mod_csv})

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing file raises FileNotFoundError."""
        paths = {"civil_frz": str(tmp_path / "nonexistent.csv")}

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Load FRZ Data
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadFrzData:
    """Test loading FRZ CSV data and creating GeoDataFrame."""

    def test_returns_geodataframe(self, civil_csv):
        """Test that function returns a GeoDataFrame."""
        result = load_frz_data(civil_csv)
        assert isinstance(result, gpd.GeoDataFrame)

    def test_correct_row_count(self, civil_csv):
        """Test that all rows are loaded."""
        result = load_frz_data(civil_csv)
        assert len(result) == 3

    def test_reprojects_to_epsg_27700(self, civil_csv):
        """Test that output is in EPSG:27700."""
        result = load_frz_data(civil_csv, target_crs="EPSG:27700")
        assert result.crs.to_epsg() == 27700

    def test_computes_frz_radius_m(self, civil_csv):
        """Test that frz_radius_m is computed correctly (km * 1000)."""
        result = load_frz_data(civil_csv)
        expected_m = result["frz_radius_km"] * 1000.0
        pd.testing.assert_series_equal(
            result["frz_radius_m"], expected_m, check_names=False
        )

    def test_geometry_is_point(self, civil_csv):
        """Test that geometries are Points."""
        result = load_frz_data(civil_csv)
        assert all(result.geometry.geom_type == "Point")

    def test_raises_on_missing_coords(self, tmp_path):
        """Test that ValueError is raised if coordinates are missing."""
        data = {
            "frz_id": ["EGR1U001A"],
            "aerodrome_name": ["TEST"],
            "operator_type": ["civil"],
            "lat_dd": [None],
            "lon_dd": [-1.0],
            "frz_radius_km": [3.704],
        }
        path = tmp_path / "bad.csv"
        pd.DataFrame(data).to_csv(path, index=False)

        with pytest.raises(ValueError, match="missing values in 'lat_dd'"):
            load_frz_data(str(path))

    def test_raises_on_missing_radius(self, tmp_path):
        """Test that ValueError is raised if radius is missing."""
        data = {
            "frz_id": ["EGR1U001A"],
            "aerodrome_name": ["TEST"],
            "operator_type": ["civil"],
            "lat_dd": [51.5],
            "lon_dd": [-1.0],
            "frz_radius_km": [None],
        }
        path = tmp_path / "bad.csv"
        pd.DataFrame(data).to_csv(path, index=False)

        with pytest.raises(ValueError, match="missing values in 'frz_radius_km'"):
            load_frz_data(str(path))


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Buffer FRZ to Circles
# ══════════════════════════════════════════════════════════════════════════════


class TestBufferFrzToCircles:
    """Test buffering FRZ points to exclusion circles."""

    @pytest.fixture
    def frz_points(self):
        """Create a simple FRZ GeoDataFrame with known radius."""
        gdf = gpd.GeoDataFrame(
            {
                "frz_id": ["TEST_A", "TEST_B"],
                "frz_radius_m": [1000.0, 2000.0],
            },
            geometry=[Point(400_000, 300_000), Point(405_000, 305_000)],
            crs="EPSG:27700",
        )
        return gdf

    def test_returns_polygons(self, frz_points):
        """Test that buffered output has Polygon geometries."""
        result = buffer_frz_to_circles(frz_points)
        assert all(result.geometry.geom_type == "Polygon")

    def test_preserves_row_count(self, frz_points):
        """Test that buffering preserves the number of entries."""
        result = buffer_frz_to_circles(frz_points)
        assert len(result) == len(frz_points)

    def test_area_approximates_pi_r_squared(self, frz_points):
        """Test that buffered circle area is approximately pi * r^2."""
        result = buffer_frz_to_circles(frz_points)
        for _, row in result.iterrows():
            expected_area = math.pi * row["frz_radius_m"] ** 2
            actual_area = row.geometry.area
            # Allow 1% tolerance for polygon approximation of circle
            assert abs(actual_area - expected_area) / expected_area < 0.01

    def test_does_not_modify_input(self, frz_points):
        """Test that the input GeoDataFrame is not modified."""
        original_geom_type = frz_points.geometry.geom_type.iloc[0]
        buffer_frz_to_circles(frz_points)
        assert frz_points.geometry.geom_type.iloc[0] == original_geom_type


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Rasterization (2-band)
# ══════════════════════════════════════════════════════════════════════════════


class TestRasterization:
    """Test rasterization of FRZ circles to 2-band raster."""

    @pytest.fixture
    def mod_circles(self):
        """MoD FRZ circles in EPSG:27700."""
        gdf = gpd.GeoDataFrame(
            {"frz_id": ["MOD_A"]},
            geometry=[Point(402_000, 302_000).buffer(1000)],
            crs="EPSG:27700",
        )
        return gdf

    @pytest.fixture
    def civil_circles(self):
        """Civilian FRZ circles in EPSG:27700 (non-overlapping with MoD)."""
        gdf = gpd.GeoDataFrame(
            {"frz_id": ["CIV_A"]},
            geometry=[Point(408_000, 308_000).buffer(1500)],
            crs="EPSG:27700",
        )
        return gdf

    @pytest.fixture
    def template(self, small_bounds):
        """Small reference grid template for test rasters."""
        return create_reference_grid(
            bounds=small_bounds, resolution=100, crs="EPSG:27700"
        )

    @pytest.fixture
    def airfield_raster(self, mod_circles, civil_circles, template):
        """Build the 2-band raster from FRZ circles."""
        band_mod = rasterize_vector(
            mod_circles, template, burn_value=1, dtype="uint8"
        )
        band_civilian = rasterize_vector(
            civil_circles, template, burn_value=1, dtype="uint8"
        )
        return np.stack([band_mod, band_civilian], axis=0)

    def test_raster_shape_is_2_bands(self, airfield_raster):
        """Test that output raster has 2 bands."""
        assert airfield_raster.shape[0] == 2

    def test_raster_is_3d(self, airfield_raster):
        """Test that output raster is 3D (bands, height, width)."""
        assert airfield_raster.ndim == 3

    def test_raster_dtype_uint8(self, airfield_raster):
        """Test that output raster uses uint8 dtype."""
        assert airfield_raster.dtype == np.uint8

    def test_binary_values_only(self, airfield_raster):
        """Test that raster contains only 0 and 1 values."""
        unique_values = np.unique(airfield_raster)
        assert set(unique_values).issubset({0, 1})

    def test_each_band_has_pixels(self, airfield_raster):
        """Test that each band contains at least some FRZ pixels."""
        for i, band_name in enumerate(POLYGON_BAND_NAMES):
            assert np.count_nonzero(airfield_raster[i]) > 0, (
                f"Band {i + 1} ({band_name}) has no FRZ pixels"
            )

    def test_bands_do_not_overlap(self, airfield_raster):
        """Test that no pixel is set in more than one band."""
        band_sum = airfield_raster.sum(axis=0)
        assert band_sum.max() <= 1, "Pixel set in multiple bands"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    @pytest.fixture
    def written_geotiff(self, tmp_path, small_bounds):
        """Build and write a 2-band FRZ GeoTIFF, return (path, raster, profile)."""
        mod_circle = gpd.GeoDataFrame(
            {"frz_id": ["MOD_A"]},
            geometry=[Point(402_000, 302_000).buffer(1000)],
            crs="EPSG:27700",
        )
        civil_circle = gpd.GeoDataFrame(
            {"frz_id": ["CIV_A"]},
            geometry=[Point(408_000, 308_000).buffer(1500)],
            crs="EPSG:27700",
        )

        width, height, transform, crs = create_reference_grid(
            bounds=small_bounds, resolution=100, crs="EPSG:27700"
        )
        template = (width, height, transform, crs)

        band_mod = rasterize_vector(
            mod_circle, template, burn_value=1, dtype="uint8"
        )
        band_civilian = rasterize_vector(
            civil_circle, template, burn_value=1, dtype="uint8"
        )
        raster = np.stack([band_mod, band_civilian], axis=0)

        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": width,
            "height": height,
            "count": 2,
            "crs": "EPSG:27700",
            "transform": transform,
        }

        output_path = tmp_path / "airfields_gb.tif"
        write_geotiff(
            raster, profile, str(output_path), band_names=POLYGON_BAND_NAMES
        )
        return str(output_path), raster, profile

    def test_output_file_exists(self, written_geotiff):
        """Test that the output GeoTIFF file was created."""
        path, _, _ = written_geotiff
        assert Path(path).exists()

    def test_readable_with_rasterio(self, written_geotiff):
        """Test that the output GeoTIFF is valid and readable."""
        path, _, _ = written_geotiff
        with rasterio.open(path) as src:
            assert src.count == 2
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

    def test_data_matches_original(self, written_geotiff):
        """Test that data read back matches original raster."""
        path, original_raster, _ = written_geotiff
        with rasterio.open(path) as src:
            read_data = src.read()
            np.testing.assert_array_equal(read_data, original_raster)

    def test_band_descriptions(self, written_geotiff):
        """Test that band descriptions are set."""
        path, _, _ = written_geotiff
        with rasterio.open(path) as src:
            descriptions = src.descriptions
            for i, expected_name in enumerate(POLYGON_BAND_NAMES):
                assert descriptions[i] == expected_name, (
                    f"Band {i + 1} description: expected "
                    f"'{expected_name}', got '{descriptions[i]}'"
                )
