"""
Unit tests for build_nuclear_exclusion_zones.py — nuclear siting hazard exclusions.

Tests the COMAH + gas pipe hazard pipeline:
- Constants (default buffer distances)
- Input validation (file existence)
- COMAH CSV loading (Point geometry creation, NaN handling)
- Zone intersection logic (boolean per-zone conflict detection)
- Full pipeline (correct output columns, dtypes, boolean logic)
- CSV round-trip (write and read back)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_nuclear_exclusion_zones import (
    DEFAULT_COMAH_BUFFER,
    DEFAULT_GAS_PIPE_BUFFER,
    build_nuclear_exclusion_zones,
    check_zone_intersections,
    load_comah_sites,
    validate_inputs,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test data (10km x 10km)."""
    return (400_000, 300_000, 410_000, 310_000)


@pytest.fixture
def comah_csv(tmp_path, small_bounds):
    """
    Synthetic COMAH CSV with 3 sites.

    Site 1 (401km, 301km): inside small_bounds, south-west quadrant
    Site 2 (408km, 308km): inside small_bounds, north-east quadrant
    Site 3 (500km, 500km): outside small_bounds entirely
    """
    df = pd.DataFrame({
        "operator_name": ["Site A", "Site B", "Site C"],
        "location_name": ["Location A", "Location B", "Location C"],
        "postcode": ["AA1 1AA", "BB2 2BB", "CC3 3CC"],
        "postcode_easting": [401_000.0, 408_000.0, 500_000.0],
        "postcode_northing": [301_000.0, 308_000.0, 500_000.0],
        "postcode_lat": [51.5, 52.0, 53.0],
        "postcode_lon": [-1.0, -0.5, 0.0],
        "country": ["England", "England", "Wales"],
    })
    path = tmp_path / "comah_test.csv"
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def comah_csv_with_nan(tmp_path):
    """COMAH CSV with one row having NaN coordinates."""
    df = pd.DataFrame({
        "operator_name": ["Site A", "Site Missing"],
        "location_name": ["Location A", "Location Missing"],
        "postcode": ["AA1 1AA", "XX0 0XX"],
        "postcode_easting": [401_000.0, np.nan],
        "postcode_northing": [301_000.0, np.nan],
        "postcode_lat": [51.5, np.nan],
        "postcode_lon": [-1.0, np.nan],
        "country": ["England", "England"],
    })
    path = tmp_path / "comah_nan.csv"
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def gas_pipe_shp(tmp_path, small_bounds):
    """
    Synthetic gas pipe shapefile with 2 polygon features.

    Pipe 1: thin strip in the north of the bounds (y=307-308km)
    Pipe 2: thin strip outside bounds entirely (y=500-501km)
    """
    xmin, ymin, xmax, ymax = small_bounds
    pipe_inside = box(xmin, 307_000, xmax, 308_000)
    pipe_outside = box(500_000, 500_000, 510_000, 501_000)

    gdf = gpd.GeoDataFrame(
        {"name": ["pipe_inside", "pipe_outside"]},
        geometry=[pipe_inside, pipe_outside],
        crs="EPSG:27700",
    )
    shp_path = tmp_path / "Gas_Pipe.shp"
    gdf.to_file(shp_path)
    return str(shp_path)


@pytest.fixture
def zones_geojson(tmp_path, small_bounds):
    """
    Synthetic zone shapes with 3 zones arranged to test intersection logic.

    Zone_A (south-west): overlaps COMAH site 1 buffer, does NOT overlap gas pipe
    Zone_B (north-east): overlaps gas pipe, overlaps COMAH site 2 buffer
    Zone_C (south-east): no overlap with any hazard (given small buffers)
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2   # 405_000
    mid_y = (ymin + ymax) / 2   # 305_000

    zone_a = box(xmin, ymin, mid_x, mid_y)      # SW: 400-405, 300-305
    zone_b = box(mid_x, mid_y, xmax, ymax)      # NE: 405-410, 305-310
    zone_c = box(mid_x, ymin, xmax, mid_y)      # SE: 405-410, 300-305

    gdf = gpd.GeoDataFrame(
        {"zone_name": ["Zone_A", "Zone_B", "Zone_C"]},
        geometry=[zone_a, zone_b, zone_c],
        crs="EPSG:27700",
    )
    path = tmp_path / "zones.geojson"
    gdf.to_file(path, driver="GeoJSON")
    return str(path)


@pytest.fixture
def simple_hazard_gdf():
    """Simple buffered hazard GeoDataFrame for intersection testing."""
    return gpd.GeoDataFrame(
        geometry=[Point(402_000, 302_000).buffer(1000)],
        crs="EPSG:27700",
    )


@pytest.fixture
def simple_zones_gdf():
    """Two zones: one overlapping the hazard, one not."""
    zone_overlap = box(401_000, 301_000, 403_000, 303_000)
    zone_clear = box(408_000, 308_000, 410_000, 310_000)
    return gpd.GeoDataFrame(
        {"zone_name": ["Overlap", "Clear"]},
        geometry=[zone_overlap, zone_clear],
        crs="EPSG:27700",
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Constants
# ══════════════════════════════════════════════════════════════════════════════


class TestConstants:
    """Test default buffer distance constants."""

    def test_default_comah_buffer(self):
        assert DEFAULT_COMAH_BUFFER == 3000

    def test_default_gas_pipe_buffer(self):
        assert DEFAULT_GAS_PIPE_BUFFER == 100


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file existence validation."""

    def test_valid_inputs_pass(self, comah_csv, gas_pipe_shp, zones_geojson):
        validate_inputs({
            "comah": comah_csv,
            "gas_pipe": gas_pipe_shp,
            "zones": zones_geojson,
        })

    def test_missing_file_raises(self, tmp_path, comah_csv, zones_geojson):
        with pytest.raises(FileNotFoundError, match="Missing 1 input"):
            validate_inputs({
                "comah": comah_csv,
                "gas_pipe": str(tmp_path / "nonexistent.shp"),
                "zones": zones_geojson,
            })

    def test_multiple_missing_files_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Missing 3 input"):
            validate_inputs({
                "comah": str(tmp_path / "a.csv"),
                "gas_pipe": str(tmp_path / "b.shp"),
                "zones": str(tmp_path / "c.geojson"),
            })


# ══════════════════════════════════════════════════════════════════════════════
# TEST: COMAH Loading
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadComahSites:
    """Test COMAH CSV loading and Point geometry creation."""

    def test_returns_geodataframe(self, comah_csv):
        gdf = load_comah_sites(comah_csv)
        assert isinstance(gdf, gpd.GeoDataFrame)

    def test_crs_is_27700(self, comah_csv):
        gdf = load_comah_sites(comah_csv)
        assert gdf.crs.to_epsg() == 27700

    def test_point_geometries(self, comah_csv):
        gdf = load_comah_sites(comah_csv)
        assert all(gdf.geometry.geom_type == "Point")

    def test_correct_number_of_sites(self, comah_csv):
        gdf = load_comah_sites(comah_csv)
        assert len(gdf) == 3

    def test_drops_nan_coordinates(self, comah_csv_with_nan):
        gdf = load_comah_sites(comah_csv_with_nan)
        assert len(gdf) == 1
        assert gdf.iloc[0]["operator_name"] == "Site A"

    def test_preserves_columns(self, comah_csv):
        gdf = load_comah_sites(comah_csv)
        assert "operator_name" in gdf.columns
        assert "postcode" in gdf.columns
        assert "country" in gdf.columns


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Zone Intersections
# ══════════════════════════════════════════════════════════════════════════════


class TestZoneIntersections:
    """Test zone intersection logic."""

    def test_returns_boolean_series(self, simple_hazard_gdf, simple_zones_gdf):
        result = check_zone_intersections(simple_hazard_gdf, simple_zones_gdf)
        assert isinstance(result, pd.Series)
        assert result.dtype == bool

    def test_overlapping_zone_is_true(self, simple_hazard_gdf, simple_zones_gdf):
        result = check_zone_intersections(simple_hazard_gdf, simple_zones_gdf)
        assert result["Overlap"] is True or result["Overlap"] == True  # noqa: E712

    def test_non_overlapping_zone_is_false(self, simple_hazard_gdf, simple_zones_gdf):
        result = check_zone_intersections(simple_hazard_gdf, simple_zones_gdf)
        assert result["Clear"] is False or result["Clear"] == False  # noqa: E712

    def test_empty_hazard_all_false(self, simple_zones_gdf):
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        result = check_zone_intersections(empty, simple_zones_gdf)
        assert not result.any()

    def test_indexed_by_zone_name(self, simple_hazard_gdf, simple_zones_gdf):
        result = check_zone_intersections(simple_hazard_gdf, simple_zones_gdf)
        assert list(result.index) == ["Overlap", "Clear"]


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Full Pipeline
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildNuclearExclusionZones:
    """Test the full build_nuclear_exclusion_zones pipeline."""

    def test_returns_dataframe(self, comah_csv, gas_pipe_shp, zones_geojson):
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        assert isinstance(result, pd.DataFrame)

    def test_output_columns(self, comah_csv, gas_pipe_shp, zones_geojson):
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        expected_cols = {
            "zone_name",
            "comah_buffer_conflict",
            "gas_pipe_buffer_conflict",
            "any_nuclear_exclusion",
        }
        assert set(result.columns) == expected_cols

    def test_boolean_dtypes(self, comah_csv, gas_pipe_shp, zones_geojson):
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        assert result["comah_buffer_conflict"].dtype == bool
        assert result["gas_pipe_buffer_conflict"].dtype == bool
        assert result["any_nuclear_exclusion"].dtype == bool

    def test_one_row_per_zone(self, comah_csv, gas_pipe_shp, zones_geojson):
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        assert len(result) == 3
        assert set(result["zone_name"]) == {"Zone_A", "Zone_B", "Zone_C"}

    def test_any_is_or_of_individual(self, comah_csv, gas_pipe_shp, zones_geojson):
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        expected_any = result["comah_buffer_conflict"] | result["gas_pipe_buffer_conflict"]
        pd.testing.assert_series_equal(
            result["any_nuclear_exclusion"],
            expected_any,
            check_names=False,
        )

    def test_comah_conflict_zone_a(self, comah_csv, gas_pipe_shp, zones_geojson):
        """Zone_A (SW) contains COMAH site 1 at (401k, 301k) — should have conflict."""
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        zone_a = result[result["zone_name"] == "Zone_A"].iloc[0]
        assert zone_a["comah_buffer_conflict"] == True  # noqa: E712

    def test_gas_pipe_conflict_zone_b(self, comah_csv, gas_pipe_shp, zones_geojson):
        """Zone_B (NE) contains gas pipe (307-308km) — should have conflict."""
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        zone_b = result[result["zone_name"] == "Zone_B"].iloc[0]
        assert zone_b["gas_pipe_buffer_conflict"] == True  # noqa: E712

    def test_no_conflict_zone_c_small_buffer(self, comah_csv, gas_pipe_shp, zones_geojson):
        """Zone_C (SE, 405-410, 300-305) — with 100m buffer, no hazards reach here."""
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=100,
            gas_pipe_buffer=100,
        )
        zone_c = result[result["zone_name"] == "Zone_C"].iloc[0]
        assert zone_c["any_nuclear_exclusion"] == False  # noqa: E712


# ══════════════════════════════════════════════════════════════════════════════
# TEST: CSV Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestCSVRoundTrip:
    """Test CSV write and read back."""

    def test_write_read_preserves_data(self, comah_csv, gas_pipe_shp, zones_geojson, tmp_path):
        result = build_nuclear_exclusion_zones(
            comah_path=comah_csv,
            gas_pipe_path=gas_pipe_shp,
            zones_path=zones_geojson,
            comah_buffer=1000,
            gas_pipe_buffer=100,
        )
        out_path = tmp_path / "exclusions.csv"
        result.to_csv(out_path, index=False)

        loaded = pd.read_csv(out_path)
        assert len(loaded) == len(result)
        assert set(loaded.columns) == set(result.columns)
        assert list(loaded["zone_name"]) == list(result["zone_name"])
