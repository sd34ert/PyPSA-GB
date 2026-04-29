"""
Unit tests for build_population_density_surface.py

Tests the population density surface building pipeline:
- Input validation (missing files raise errors)
- Scotland OA loading (density from Popcount/sqkm)
- England & Wales OA loading (density joined from CSV)
- GB-wide combine and rasterization
- Edge cases (missing columns, zero area, failed joins)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from shapely.geometry import box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_population_density_surface import (
    build_population_density_surface,
    load_england_wales_oa,
    load_scotland_oa,
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
def scotland_oa_gpkg(tmp_path, small_bounds):
    """
    Create a synthetic Scotland OA GeoPackage with embedded population counts.

    Contains 4 OAs covering the test area, each with Popcount and sqkm columns.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    geometries = [
        box(xmin, mid_y, mid_x, ymax),  # top-left: urban
        box(mid_x, mid_y, xmax, ymax),  # top-right: suburban
        box(xmin, ymin, mid_x, mid_y),  # bottom-left: rural
        box(mid_x, ymin, xmax, mid_y),  # bottom-right: very rural
    ]

    gdf = gpd.GeoDataFrame(
        {
            "code": ["S00100001", "S00100002", "S00100003", "S00100004"],
            "Popcount": [500, 200, 50, 10],
            "sqkm": [0.25, 0.25, 0.25, 0.25],
            "HHcount": [200, 80, 20, 4],
            "council": ["Glasgow", "Glasgow", "Highland", "Highland"],
        },
        geometry=geometries,
        crs="EPSG:27700",
    )

    path = tmp_path / "output_areas_scotland.gpkg"
    gdf.to_file(path, driver="GPKG")
    return str(path)


@pytest.fixture
def ew_oa_gpkg(tmp_path, small_bounds):
    """
    Create a synthetic E&W OA GeoPackage with boundaries only (no population).

    Contains 4 OAs covering the test area, with OA21CD codes for CSV join.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    geometries = [
        box(xmin, mid_y, mid_x, ymax),
        box(mid_x, mid_y, xmax, ymax),
        box(xmin, ymin, mid_x, mid_y),
        box(mid_x, ymin, xmax, mid_y),
    ]

    gdf = gpd.GeoDataFrame(
        {
            "OA21CD": ["E00000001", "E00000002", "W00000001", "W00000002"],
            "LSOA21CD": ["E01000001", "E01000002", "W01000001", "W01000002"],
            "BNG_E": [400250, 400750, 400250, 400750],
            "BNG_N": [300750, 300750, 300250, 300250],
        },
        geometry=geometries,
        crs="EPSG:27700",
    )

    path = tmp_path / "output_areas_ew.gpkg"
    gdf.to_file(path, driver="GPKG")
    return str(path)


@pytest.fixture
def ew_density_csv(tmp_path):
    """
    Create a synthetic Census TS006 density CSV matching the E&W OA codes.

    Columns match ONS NOMIS TS006 format: Output Areas Code, Output Areas, Observation.
    """
    df = pd.DataFrame(
        {
            "Output Areas Code": ["E00000001", "E00000002", "W00000001", "W00000002"],
            "Output Areas": [
                "London OA 1",
                "London OA 2",
                "Cardiff OA 1",
                "Cardiff OA 2",
            ],
            "Observation": [5000.0, 3000.0, 1500.0, 800.0],
        }
    )

    path = tmp_path / "output_areas_ew.csv"
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def ew_density_csv_partial(tmp_path):
    """
    Create a density CSV with only 2 of the 4 expected OA codes.

    Used to test join validation (should raise ValueError).
    """
    df = pd.DataFrame(
        {
            "Output Areas Code": ["E00000001", "E00000002"],
            "Output Areas": ["London OA 1", "London OA 2"],
            "Observation": [5000.0, 3000.0],
        }
    )

    path = tmp_path / "output_areas_ew_partial.csv"
    df.to_csv(path, index=False)
    return str(path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, scotland_oa_gpkg, ew_oa_gpkg, ew_density_csv):
        """Test that validation passes when all files exist."""
        paths = {
            "oa_shapes_sco": scotland_oa_gpkg,
            "oa_shapes_ew": ew_oa_gpkg,
            "oa_density_ew": ew_density_csv,
        }
        validate_inputs(paths)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "oa_shapes_sco": str(tmp_path / "nonexistent.gpkg"),
            "oa_shapes_ew": str(tmp_path / "also_missing.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, scotland_oa_gpkg, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = {
            "oa_shapes_sco": scotland_oa_gpkg,
            "oa_shapes_ew": str(tmp_path / "missing.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Scotland OA Loading
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadScotlandOA:
    """Test Scotland OA loading and density calculation."""

    def test_returns_geodataframe_with_density(self, scotland_oa_gpkg):
        """Test that function returns a GeoDataFrame with geometry and density columns."""
        result = load_scotland_oa(scotland_oa_gpkg)

        assert isinstance(result, gpd.GeoDataFrame)
        assert "geometry" in result.columns
        assert "density" in result.columns
        assert len(result.columns) == 2

    def test_density_calculated_correctly(self, scotland_oa_gpkg):
        """Test that density = Popcount / sqkm."""
        result = load_scotland_oa(scotland_oa_gpkg)

        # First OA: 500 / 0.25 = 2000 people/km²
        assert result["density"].iloc[0] == pytest.approx(2000.0)
        # Last OA: 10 / 0.25 = 40 people/km²
        assert result["density"].iloc[3] == pytest.approx(40.0)

    def test_crs_is_epsg_27700(self, scotland_oa_gpkg):
        """Test that output CRS is EPSG:27700."""
        result = load_scotland_oa(scotland_oa_gpkg)

        assert result.crs.to_epsg() == 27700

    def test_correct_feature_count(self, scotland_oa_gpkg):
        """Test that all OAs are returned."""
        result = load_scotland_oa(scotland_oa_gpkg)

        assert len(result) == 4

    def test_missing_popcount_column_raises(self, tmp_path, small_bounds):
        """Test that missing Popcount column raises ValueError."""
        xmin, ymin, xmax, ymax = small_bounds
        gdf = gpd.GeoDataFrame(
            {"code": ["S001"], "sqkm": [0.25]},
            geometry=[box(xmin, ymin, xmax, ymax)],
            crs="EPSG:27700",
        )
        path = tmp_path / "bad_scotland.gpkg"
        gdf.to_file(path, driver="GPKG")

        with pytest.raises(ValueError, match="missing required columns"):
            load_scotland_oa(str(path))

    def test_missing_sqkm_column_raises(self, tmp_path, small_bounds):
        """Test that missing sqkm column raises ValueError."""
        xmin, ymin, xmax, ymax = small_bounds
        gdf = gpd.GeoDataFrame(
            {"code": ["S001"], "Popcount": [100]},
            geometry=[box(xmin, ymin, xmax, ymax)],
            crs="EPSG:27700",
        )
        path = tmp_path / "bad_scotland.gpkg"
        gdf.to_file(path, driver="GPKG")

        with pytest.raises(ValueError, match="missing required columns"):
            load_scotland_oa(str(path))

    def test_zero_sqkm_raises(self, tmp_path, small_bounds):
        """Test that zero sqkm value raises ValueError (division by zero)."""
        xmin, ymin, xmax, ymax = small_bounds
        gdf = gpd.GeoDataFrame(
            {"code": ["S001"], "Popcount": [100], "sqkm": [0.0]},
            geometry=[box(xmin, ymin, xmax, ymax)],
            crs="EPSG:27700",
        )
        path = tmp_path / "zero_area_scotland.gpkg"
        gdf.to_file(path, driver="GPKG")

        with pytest.raises(ValueError, match="sqkm=0"):
            load_scotland_oa(str(path))

    def test_nonexistent_file_raises(self, tmp_path):
        """Test that nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_scotland_oa(str(tmp_path / "nonexistent.gpkg"))


# ══════════════════════════════════════════════════════════════════════════════
# TEST: England & Wales OA Loading
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadEnglandWalesOA:
    """Test England & Wales OA loading and CSV join."""

    def test_returns_geodataframe_with_density(self, ew_oa_gpkg, ew_density_csv):
        """Test that function returns a GeoDataFrame with geometry and density columns."""
        result = load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

        assert isinstance(result, gpd.GeoDataFrame)
        assert "geometry" in result.columns
        assert "density" in result.columns
        assert len(result.columns) == 2

    def test_density_joined_correctly(self, ew_oa_gpkg, ew_density_csv):
        """Test that density values from CSV are joined to the correct OAs."""
        result = load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

        # CSV has: E00000001=5000, E00000002=3000, W00000001=1500, W00000002=800
        assert result["density"].iloc[0] == pytest.approx(5000.0)
        assert result["density"].iloc[1] == pytest.approx(3000.0)
        assert result["density"].iloc[2] == pytest.approx(1500.0)
        assert result["density"].iloc[3] == pytest.approx(800.0)

    def test_no_missing_density_after_join(self, ew_oa_gpkg, ew_density_csv):
        """Test that all OAs have density values after join."""
        result = load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

        assert result["density"].notna().all()

    def test_crs_is_epsg_27700(self, ew_oa_gpkg, ew_density_csv):
        """Test that output CRS is EPSG:27700."""
        result = load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

        assert result.crs.to_epsg() == 27700

    def test_correct_feature_count(self, ew_oa_gpkg, ew_density_csv):
        """Test that all OAs are returned."""
        result = load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

        assert len(result) == 4

    def test_partial_csv_raises(self, ew_oa_gpkg, ew_density_csv_partial):
        """Test that incomplete CSV join raises ValueError."""
        with pytest.raises(ValueError, match="Join failed.*missing density"):
            load_england_wales_oa(ew_oa_gpkg, ew_density_csv_partial)

    def test_missing_oa21cd_column_raises(self, tmp_path, small_bounds, ew_density_csv):
        """Test that GPKG without OA21CD column raises ValueError."""
        xmin, ymin, xmax, ymax = small_bounds
        gdf = gpd.GeoDataFrame(
            {"wrong_col": ["E001"]},
            geometry=[box(xmin, ymin, xmax, ymax)],
            crs="EPSG:27700",
        )
        path = tmp_path / "bad_ew.gpkg"
        gdf.to_file(path, driver="GPKG")

        with pytest.raises(ValueError, match="missing 'OA21CD' column"):
            load_england_wales_oa(str(path), ew_density_csv)

    def test_missing_csv_columns_raises(self, ew_oa_gpkg, tmp_path):
        """Test that CSV missing required columns raises ValueError."""
        bad_csv = tmp_path / "bad_density.csv"
        pd.DataFrame({"wrong": [1]}).to_csv(bad_csv, index=False)

        with pytest.raises(ValueError, match="missing required columns"):
            load_england_wales_oa(ew_oa_gpkg, str(bad_csv))

    def test_nonexistent_gpkg_raises(self, tmp_path, ew_density_csv):
        """Test that nonexistent GPKG raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_england_wales_oa(str(tmp_path / "nonexistent.gpkg"), ew_density_csv)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Population Density Surface
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildPopulationDensitySurface:
    """Test the main raster building function."""

    @pytest.fixture
    def oa_sco(self, scotland_oa_gpkg):
        """Load Scotland OA fixture via the production function."""
        return load_scotland_oa(scotland_oa_gpkg)

    @pytest.fixture
    def oa_ew(self, ew_oa_gpkg, ew_density_csv):
        """Load E&W OA fixture via the production function."""
        return load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

    def test_returns_raster_and_profile(self, oa_sco, oa_ew):
        """Test that function returns (ndarray, dict) tuple."""
        raster, profile = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_raster_is_2d(self, oa_sco, oa_ew):
        """Test that output raster is single-band (2D)."""
        raster, _ = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        assert raster.ndim == 2

    def test_raster_dtype_float32(self, oa_sco, oa_ew):
        """Test that output raster uses float32 dtype."""
        raster, profile = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        assert raster.dtype == np.float32
        assert profile["dtype"] == "float32"

    def test_raster_has_valid_density_values(self, oa_sco, oa_ew):
        """Test that raster contains plausible density values."""
        raster, _ = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        valid = raster[raster != -9999.0]
        assert len(valid) > 0
        assert valid.min() >= 0.0

    def test_raster_contains_nodata(self, oa_sco, oa_ew):
        """Test that raster has nodata pixels outside OA coverage."""
        raster, _ = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        # The canonical GB grid is much larger than our 1km test area,
        # so most pixels should be nodata
        nodata_count = np.count_nonzero(raster == -9999.0)
        assert nodata_count > 0

    def test_profile_has_required_keys(self, oa_sco, oa_ew):
        """Test that the rasterio profile has all required keys."""
        _, profile = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        required_keys = {"driver", "dtype", "width", "height", "count", "crs", "transform"}
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 1
        assert profile["crs"] == "EPSG:27700"

    def test_combines_both_nations(self, oa_sco, oa_ew):
        """Test that both Scotland and E&W OAs contribute to the raster."""
        raster, _ = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        valid = raster[raster != -9999.0]
        unique_values = np.unique(valid)

        # Should have multiple distinct density values from both nations
        assert len(unique_values) > 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, scotland_oa_gpkg, ew_oa_gpkg, ew_density_csv):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        oa_sco = load_scotland_oa(scotland_oa_gpkg)
        oa_ew = load_england_wales_oa(ew_oa_gpkg, ew_density_csv)

        raster, profile = build_population_density_surface(
            oa_sco=oa_sco,
            oa_ew=oa_ew,
            resolution=100,
        )

        output_path = tmp_path / "population_density.tif"
        write_geotiff(raster, profile, str(output_path))

        with rasterio.open(output_path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "float32"

            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, raster)
