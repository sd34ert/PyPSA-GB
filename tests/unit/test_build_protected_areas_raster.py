"""
Unit tests for build_protected_areas_raster.py

Tests the protected areas raster building pipeline:
- Input validation (missing files raise errors)
- Tier merging logic (overlapping geometries dissolve correctly)
- Rasterization produces correct 4-band output
- Zone fractions are in valid range [0.0, 1.0]
- Edge cases (empty geometries, non-overlapping tiers)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from shapely.geometry import box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_protected_areas_raster import (
    build_protected_areas_raster,
    calculate_zone_fractions,
    load_and_merge_tier,
    validate_inputs,
)
from scripts.utilities.land_utils import (
    rasterize_vector,
    write_geotiff,
)

# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test rasters (1km x 1km)."""
    return (400_000, 300_000, 401_000, 301_000)


@pytest.fixture
def small_template(small_bounds):
    """Reference grid template for a small test area at 100m resolution."""
    xmin, ymin, xmax, ymax = small_bounds
    resolution = 100
    width = int((xmax - xmin) / resolution)
    height = int((ymax - ymin) / resolution)
    transform = Affine.translation(xmin, ymax) * Affine.scale(resolution, -resolution)
    return (width, height, transform, "EPSG:27700")


@pytest.fixture
def sample_polygon_gdf(small_bounds):
    """
    Create a GeoDataFrame with a single polygon covering 25% of the test area.

    The polygon covers the top-left quarter of the 1km x 1km test area.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2
    geom = box(xmin, mid_y, mid_x, ymax)  # top-left quarter
    return gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")


@pytest.fixture
def overlapping_polygons_gdf(small_bounds):
    """
    Create a GeoDataFrame with two overlapping polygons.

    Polygon A covers the left half, polygon B covers the top half.
    Their overlap is the top-left quarter.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2
    poly_a = box(xmin, ymin, mid_x, ymax)  # left half
    poly_b = box(xmin, mid_y, xmax, ymax)  # top half
    return gpd.GeoDataFrame(geometry=[poly_a, poly_b], crs="EPSG:27700")


@pytest.fixture
def zone_shapes_gdf(small_bounds):
    """
    Create zone shapes splitting the test area into two zones.

    Zone A = left half, Zone B = right half.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    zone_a = box(xmin, ymin, mid_x, ymax)
    zone_b = box(mid_x, ymin, xmax, ymax)
    return gpd.GeoDataFrame(
        {"zone_name": ["Zone_A", "Zone_B"], "geometry": [zone_a, zone_b]},
        crs="EPSG:27700",
    )


@pytest.fixture
def synthetic_vector_files(tmp_path, small_bounds):
    """
    Create 24 synthetic vector files matching the Snakemake rule inputs.

    Each tier gets geometries in different parts of the test area so
    the output bands are distinguishable.
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    # Tier 1 geometries: top-left quarter
    t1_geom = box(xmin, mid_y, mid_x, ymax)
    # Tier 2 geometries: top-right quarter
    t2_geom = box(mid_x, mid_y, xmax, ymax)
    # Tier 3 geometries: bottom-left quarter
    t3_geom = box(xmin, ymin, mid_x, mid_y)
    # Tier 4 geometries: bottom-right quarter
    t4_geom = box(mid_x, ymin, xmax, mid_y)

    def _write_gpkg(name, geom):
        path = tmp_path / f"{name}.gpkg"
        gdf = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:27700")
        gdf.to_file(path, driver="GPKG")
        return str(path)

    files = {
        # Tier 1: SAC/SPA/Ramsar/SSSI (excluding marine)
        "sac": _write_gpkg("sac", t1_geom),
        "spa": _write_gpkg("spa", t1_geom),
        "ramsar": _write_gpkg("ramsar", t1_geom),
        "sssi_eng": _write_gpkg("sssi_eng", t1_geom),
        "sssi_sco": _write_gpkg("sssi_sco", t1_geom),
        "sssi_wal": _write_gpkg("sssi_wal", t1_geom),
        # Tier 2: AONB/NatParks/NSA
        "aonb_eng": _write_gpkg("aonb_eng", t2_geom),
        "aonb_wal": _write_gpkg("aonb_wal", t2_geom),
        "nsa_sco": _write_gpkg("nsa_sco", t2_geom),
        "natpark_eng": _write_gpkg("natpark_eng", t2_geom),
        "natpark_wal": _write_gpkg("natpark_wal", t2_geom),
        "natpark_sco": _write_gpkg("natpark_sco", t2_geom),
        # Tier 3: Irreplaceable habitats
        "aw_eng": _write_gpkg("aw_eng", t3_geom),
        "aw_sco": _write_gpkg("aw_sco", t3_geom),
        "aw_wal": _write_gpkg("aw_wal", t3_geom),
        "irr_eng": _write_gpkg("irr_eng", t3_geom),
        "irr_sco": _write_gpkg("irr_sco", t3_geom),
        "irr_bog_wal": _write_gpkg("irr_bog_wal", t3_geom),
        "irr_dunes_wal": _write_gpkg("irr_dunes_wal", t3_geom),
        "irr_lime_wal": _write_gpkg("irr_lime_wal", t3_geom),
        "irr_fens_wal": _write_gpkg("irr_fens_wal", t3_geom),
        # Tier 4: Historic environment
        "hist_eng": _write_gpkg("hist_eng", t4_geom),
        "hist_sco": _write_gpkg("hist_sco", t4_geom),
        "hist_wal": _write_gpkg("hist_wal", t4_geom),
    }

    return files


@pytest.fixture
def zone_shapes_file(tmp_path, small_bounds):
    """Write zone shapes to a GeoPackage for testing."""
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    zone_a = box(xmin, ymin, mid_x, ymax)
    zone_b = box(mid_x, ymin, xmax, ymax)
    gdf = gpd.GeoDataFrame(
        {"zone_name": ["Zone_A", "Zone_B"], "geometry": [zone_a, zone_b]},
        crs="EPSG:27700",
    )
    path = tmp_path / "zones.gpkg"
    gdf.to_file(path, driver="GPKG")
    return str(path)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

TIER1_KEYS = ("sac", "spa", "ramsar", "sssi_eng", "sssi_sco", "sssi_wal")
TIER2_KEYS = (
    "aonb_eng", "aonb_wal", "nsa_sco",
    "natpark_eng", "natpark_wal", "natpark_sco",
)
TIER3_KEYS = (
    "aw_eng", "aw_sco", "aw_wal",
    "irr_eng", "irr_sco",
    "irr_bog_wal", "irr_dunes_wal", "irr_lime_wal", "irr_fens_wal",
)
TIER4_KEYS = ("hist_eng", "hist_sco", "hist_wal")


def _build_tier_lists(files):
    """Build tier path lists from synthetic_vector_files dict."""
    return (
        [files[k] for k in TIER1_KEYS],
        [files[k] for k in TIER2_KEYS],
        [files[k] for k in TIER3_KEYS],
        [files[k] for k in TIER4_KEYS],
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_all_exist(self, synthetic_vector_files):
        """Test that validation passes when all files exist."""
        validate_inputs(synthetic_vector_files)

    def test_validate_inputs_missing_file_raises(self, tmp_path):
        """Test that missing files raise FileNotFoundError."""
        paths = {
            "sac": str(tmp_path / "nonexistent.gpkg"),
            "spa": str(tmp_path / "also_missing.gpkg"),
        }

        with pytest.raises(FileNotFoundError, match="Missing 2 input file"):
            validate_inputs(paths)

    def test_validate_inputs_partial_missing(self, synthetic_vector_files, tmp_path):
        """Test that partially missing files are reported correctly."""
        paths = synthetic_vector_files.copy()
        paths["extra_missing"] = str(tmp_path / "nope.gpkg")

        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs(paths)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Tier Merging and Dissolve
# ══════════════════════════════════════════════════════════════════════════════


class TestTierMerging:
    """Test tier loading, merging, and dissolve logic."""

    def test_load_and_merge_single_file(self, tmp_path, sample_polygon_gdf):
        """Test merging a single file tier."""
        path = tmp_path / "single.gpkg"
        sample_polygon_gdf.to_file(path, driver="GPKG")

        result = load_and_merge_tier([str(path)], "Test Tier")

        assert isinstance(result, gpd.GeoDataFrame)
        assert not result.empty
        assert result.crs.to_epsg() == 27700

    def test_load_and_merge_multiple_files(self, tmp_path, small_bounds):
        """Test merging multiple files into a single tier."""
        xmin, ymin, xmax, ymax = small_bounds
        mid_x = (xmin + xmax) / 2

        gdf_a = gpd.GeoDataFrame(geometry=[box(xmin, ymin, mid_x, ymax)], crs="EPSG:27700")
        gdf_b = gpd.GeoDataFrame(geometry=[box(mid_x, ymin, xmax, ymax)], crs="EPSG:27700")

        path_a = tmp_path / "a.gpkg"
        path_b = tmp_path / "b.gpkg"
        gdf_a.to_file(path_a, driver="GPKG")
        gdf_b.to_file(path_b, driver="GPKG")

        result = load_and_merge_tier([str(path_a), str(path_b)], "Test Tier")

        assert isinstance(result, gpd.GeoDataFrame)
        assert not result.empty

    def test_overlapping_geometries_dissolve(self, tmp_path, overlapping_polygons_gdf):
        """Test that overlapping geometries are dissolved correctly."""
        path = tmp_path / "overlap.gpkg"
        overlapping_polygons_gdf.to_file(path, driver="GPKG")

        result = load_and_merge_tier([str(path)], "Overlap Tier")

        # After dissolve, the union of the two overlapping polygons should
        # produce fewer or equal features, with no double-counting area
        total_area = result.geometry.area.sum()
        original_area = overlapping_polygons_gdf.geometry.union_all().area
        assert abs(total_area - original_area) < 1.0  # within 1 sq metre


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Raster Output
# ══════════════════════════════════════════════════════════════════════════════


class TestRasterOutput:
    """Test raster building and output properties."""

    def test_output_has_four_bands(self, synthetic_vector_files):
        """Test that the output raster has exactly 4 bands."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        raster, profile = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        assert raster.ndim == 3
        assert raster.shape[0] == 4

    def test_output_dtype_uint8(self, synthetic_vector_files):
        """Test that the output raster uses uint8 dtype."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        raster, profile = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        assert raster.dtype == np.uint8
        assert profile["dtype"] == "uint8"

    def test_binary_values_only(self, synthetic_vector_files):
        """Test that raster contains only 0 and 1 values."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        raster, _ = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        unique_values = np.unique(raster)
        assert set(unique_values).issubset({0, 1})

    def test_each_band_has_nonzero_pixels(self, synthetic_vector_files):
        """Test that each band contains at least some protected pixels."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        raster, _ = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        for band_idx in range(4):
            assert np.count_nonzero(raster[band_idx]) > 0, (
                f"Band {band_idx} has no protected pixels"
            )

    def test_profile_has_required_keys(self, synthetic_vector_files):
        """Test that the rasterio profile has all required keys."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        _, profile = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        required_keys = {"driver", "dtype", "width", "height", "count", "crs", "transform"}
        assert required_keys.issubset(set(profile.keys()))
        assert profile["count"] == 4
        assert profile["crs"] == "EPSG:27700"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Write / Read Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    def test_written_geotiff_readable(self, tmp_path, synthetic_vector_files):
        """Test that the output GeoTIFF is valid and readable with rasterio."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        raster, profile = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        output_path = tmp_path / "protected_areas.tif"
        band_names = [
            "SAC_SPA_Ramsar_SSSI",
            "AONB_NatParks_NSA",
            "Irreplaceable_Habitats",
            "Historic_Environment",
        ]
        write_geotiff(raster, profile, str(output_path), band_names=band_names)

        with rasterio.open(output_path) as src:
            assert src.count == 4
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

            for i in range(1, 5):
                assert src.descriptions[i - 1] == band_names[i - 1]

            read_data = src.read()
            np.testing.assert_array_equal(read_data, raster)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Zone Fractions
# ══════════════════════════════════════════════════════════════════════════════


class TestZoneFractions:
    """Test zone fraction calculation."""

    def test_fractions_in_valid_range(self, small_template, sample_polygon_gdf, zone_shapes_gdf):
        """Test that all zone fractions are between 0.0 and 1.0."""
        _, _, transform, _ = small_template

        band = rasterize_vector(sample_polygon_gdf, small_template)
        raster = np.stack([band, band, band, band], axis=0)

        fractions = calculate_zone_fractions(
            raster=raster,
            transform=transform,
            zones=zone_shapes_gdf,
        )

        for col in ["tier1_frac", "tier2_frac", "tier3_frac", "tier4_frac"]:
            assert col in fractions.columns
            assert fractions[col].min() >= 0.0
            assert fractions[col].max() <= 1.0

    def test_fractions_have_correct_columns(
        self, small_template, sample_polygon_gdf, zone_shapes_gdf
    ):
        """Test that output DataFrame has expected columns."""
        _, _, transform, _ = small_template

        band = rasterize_vector(sample_polygon_gdf, small_template)
        raster = np.stack([band, band, band, band], axis=0)

        fractions = calculate_zone_fractions(
            raster=raster,
            transform=transform,
            zones=zone_shapes_gdf,
        )

        expected_columns = {"zone", "tier1_frac", "tier2_frac", "tier3_frac", "tier4_frac"}
        assert set(fractions.columns) == expected_columns

    def test_fractions_match_zone_count(self, small_template, sample_polygon_gdf, zone_shapes_gdf):
        """Test that output has one row per zone."""
        _, _, transform, _ = small_template

        band = rasterize_vector(sample_polygon_gdf, small_template)
        raster = np.stack([band, band, band, band], axis=0)

        fractions = calculate_zone_fractions(
            raster=raster,
            transform=transform,
            zones=zone_shapes_gdf,
        )

        assert len(fractions) == len(zone_shapes_gdf)

    def test_zone_with_no_coverage_has_zero_fraction(self, small_template, zone_shapes_gdf):
        """Test that a zone with no protected area has fraction 0.0."""
        width, height, transform, crs = small_template

        # All-zero raster — no protected areas anywhere
        empty_band = np.zeros((height, width), dtype="uint8")
        raster = np.stack([empty_band, empty_band, empty_band, empty_band], axis=0)

        fractions = calculate_zone_fractions(
            raster=raster,
            transform=transform,
            zones=zone_shapes_gdf,
        )

        for col in ["tier1_frac", "tier2_frac", "tier3_frac", "tier4_frac"]:
            assert (fractions[col] == 0.0).all()

    def test_zone_fully_covered_has_fraction_one(self, small_template, zone_shapes_gdf):
        """Test that a zone fully covered by protection has fraction ~1.0."""
        width, height, transform, crs = small_template

        # All-ones raster — entire area protected
        full_band = np.ones((height, width), dtype="uint8")
        raster = np.stack([full_band, full_band, full_band, full_band], axis=0)

        fractions = calculate_zone_fractions(
            raster=raster,
            transform=transform,
            zones=zone_shapes_gdf,
        )

        for col in ["tier1_frac", "tier2_frac", "tier3_frac", "tier4_frac"]:
            assert (fractions[col] >= 0.99).all()

    def test_fractions_from_file(self, small_template, sample_polygon_gdf, zone_shapes_file):
        """Test zone fractions loaded from a file path."""
        _, _, transform, _ = small_template

        band = rasterize_vector(sample_polygon_gdf, small_template)
        raster = np.stack([band, band, band, band], axis=0)

        fractions = calculate_zone_fractions(
            raster=raster,
            transform=transform,
            zone_path=zone_shapes_file,
        )

        assert not fractions.empty
        assert "zone" in fractions.columns

    def test_fractions_requires_zone_source(self, small_template):
        """Test that calculate_zone_fractions raises when no zone source given."""
        width, height, transform, _ = small_template
        raster = np.zeros((4, height, width), dtype="uint8")

        with pytest.raises(ValueError, match="Either zone_path or zones must be provided"):
            calculate_zone_fractions(raster=raster, transform=transform)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Partial Coverage
# ══════════════════════════════════════════════════════════════════════════════


class TestPartialCoverage:
    """Test that different tiers produce spatially distinct bands."""

    def test_tiers_have_distinct_spatial_coverage(self, synthetic_vector_files):
        """Test that different tiers cover different parts of the area."""
        tier1, tier2, tier3, tier4 = _build_tier_lists(synthetic_vector_files)

        raster, _ = build_protected_areas_raster(
            tier1_paths=tier1,
            tier2_paths=tier2,
            tier3_paths=tier3,
            tier4_paths=tier4,
            resolution=100,
            target_crs=27700,
        )

        # Each tier's geometries are in different quarters, so bands
        # should not be identical
        for i in range(4):
            for j in range(i + 1, 4):
                assert not np.array_equal(raster[i], raster[j]), (
                    f"Band {i} and Band {j} are identical — tiers should be spatially distinct"
                )
