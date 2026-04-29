"""
Unit tests for build_nuclear_smr_exclusions.py — pixel-level SMR exclusion raster.

Tests the 13-step exclusion pipeline:
- Constants (default buffer distances)
- Shared foundation helpers (protected areas, airfields, land cover,
  flooding, groundwater SPZ, coastal erosion, ALC BMV, Green Belt)
- Nuclear-specific helpers (Scotland ban, population criterion,
  COMAH buffer rasterization, gas pipe buffer rasterization,
  water availability placeholder)
- Full pipeline (raster output, CSV output, per-zone fractions)
- Config-driven enable/disable of each layer
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import Point, box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_nuclear_smr_exclusions import (
    DEFAULT_COMAH_BUFFER,
    DEFAULT_GAS_PIPE_BUFFER,
    apply_alc_bmv_exclusion,
    apply_airfield_exclusions,
    apply_coastal_change_exclusion,
    apply_comah_exclusion,
    apply_flooding_exclusion,
    apply_gas_pipe_exclusion,
    apply_green_belt_exclusion,
    apply_groundwater_exclusion,
    apply_land_cover_exclusions,
    apply_pop_criterion_exclusion,
    apply_protected_area_exclusions,
    apply_raster_buffer,
    apply_scotland_exclusion,
    apply_water_constraint,
    buffer_distance_to_pixels,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES — Synthetic rasters
# ══════════════════════════════════════════════════════════════════════════════

# Small test grid: 10x10 pixels, 100m resolution, covering 1km x 1km
GRID_WIDTH = 10
GRID_HEIGHT = 10
RESOLUTION = 100  # metres per pixel
XMIN, YMIN = 400_000, 300_000
XMAX = XMIN + GRID_WIDTH * RESOLUTION   # 401_000
YMAX = YMIN + GRID_HEIGHT * RESOLUTION  # 301_000
TEST_TRANSFORM = from_bounds(XMIN, YMIN, XMAX, YMAX, GRID_WIDTH, GRID_HEIGHT)
TEST_CRS = "EPSG:27700"


def _write_uint8_tif(path, data, band_count=1):
    """Write a uint8 GeoTIFF with test grid parameters."""
    if data.ndim == 2:
        data = data[np.newaxis, ...]
    with rasterio.open(
        path, "w",
        driver="GTiff",
        width=GRID_WIDTH,
        height=GRID_HEIGHT,
        count=data.shape[0],
        dtype="uint8",
        crs=TEST_CRS,
        transform=TEST_TRANSFORM,
        nodata=255,
    ) as dst:
        for i in range(data.shape[0]):
            dst.write(data[i], i + 1)


@pytest.fixture
def protected_tif(tmp_path):
    """4-band protected areas raster. Band 1: top-left quadrant excluded."""
    data = np.zeros((4, GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    # Band 1 (tier1): top-left 5x5 quadrant
    data[0, :5, :5] = 1
    # Band 2 (tier2): single pixel
    data[1, 0, 9] = 1
    # Band 3 (tier3): empty
    # Band 4 (tier4): bottom row
    data[3, 9, :] = 1
    path = tmp_path / "protected_areas_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def airfields_tif(tmp_path):
    """2-band airfield raster. Band 1 (MoD): row 7. Band 2 (Civil): row 8."""
    data = np.zeros((2, GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[0, 7, :] = 1
    data[1, 8, :] = 1
    path = tmp_path / "airfields_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def land_cover_tif(tmp_path):
    """Single-band land cover. Code 20 (urban) in col 0, code 3 (arable) elsewhere."""
    data = np.full((GRID_HEIGHT, GRID_WIDTH), 3, dtype="uint8")
    data[:, 0] = 20  # urban column
    data[0, 5] = 14  # one freshwater pixel
    path = tmp_path / "land_cover_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def flooding_tif(tmp_path):
    """Single-band flooding raster. Bottom-right 3x3 corner flooded."""
    data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[7:, 7:] = 1
    path = tmp_path / "flooding_risk_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def groundwater_tif(tmp_path):
    """3-band SPZ raster. Band 1 (SPZ1): row 5. Band 2 (SPZ2/3): row 6."""
    data = np.zeros((3, GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[0, 5, :] = 1  # SPZ1
    data[1, 6, :] = 1  # SPZ2/3
    path = tmp_path / "groundwater_spz_ew.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def coastal_erosion_tif(tmp_path):
    """Single-band coastal erosion. Two pixels at (0,0) and (0,1)."""
    data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[0, 0] = 1
    data[0, 1] = 1
    path = tmp_path / "coastal_erosion_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def alc_bmv_tif(tmp_path):
    """Single-band ALC BMV. Right half excluded."""
    data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[:, 5:] = 1
    path = tmp_path / "alc_bmv_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def green_belt_tif(tmp_path):
    """Single-band Green Belt. Centre 4x4 block excluded."""
    data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[3:7, 3:7] = 1
    path = tmp_path / "green_belt_gb.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def scotland_mask_tif(tmp_path):
    """Scotland mask. Top half (rows 0-4) = Scotland."""
    data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[:5, :] = 1
    path = tmp_path / "scotland_mask_Zonal.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def pop_criterion_tif(tmp_path):
    """Population criterion. 0=eligible, 1=ineligible. Left half ineligible."""
    data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
    data[:, :5] = 1  # left half ineligible
    path = tmp_path / "nuclear_pop_criterion_Zonal.tif"
    _write_uint8_tif(path, data)
    return str(path)


@pytest.fixture
def comah_csv(tmp_path):
    """COMAH CSV with 2 sites: one inside test grid, one outside."""
    df = pd.DataFrame({
        "operator_name": ["Site A", "Site B"],
        "postcode_easting": [400_500.0, 500_000.0],
        "postcode_northing": [300_500.0, 500_000.0],
    })
    path = tmp_path / "comah_test.csv"
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def gas_pipe_shp(tmp_path):
    """Gas pipe shapefile with one thin strip inside test grid."""
    pipe = box(XMIN, 300_400, XMAX, 300_600)  # 200m wide strip
    gdf = gpd.GeoDataFrame(
        {"name": ["pipe1"]},
        geometry=[pipe],
        crs=TEST_CRS,
    )
    path = tmp_path / "Gas_Pipe.shp"
    gdf.to_file(path)
    return str(path)


@pytest.fixture
def template():
    """Reference grid template tuple."""
    return (GRID_WIDTH, GRID_HEIGHT, TEST_TRANSFORM, TEST_CRS)


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
# TEST: Buffer Helpers
# ══════════════════════════════════════════════════════════════════════════════


class TestBufferHelpers:
    """Test buffer distance conversion and raster buffering."""

    def test_buffer_distance_to_pixels_100m(self):
        assert buffer_distance_to_pixels(100, 100) == 1

    def test_buffer_distance_to_pixels_3000m(self):
        assert buffer_distance_to_pixels(3000, 100) == 30

    def test_buffer_distance_to_pixels_zero(self):
        assert buffer_distance_to_pixels(0, 100) == 0

    def test_buffer_distance_to_pixels_negative(self):
        assert buffer_distance_to_pixels(-50, 100) == 0

    def test_apply_raster_buffer_expands(self):
        mask = np.zeros((10, 10), dtype="uint8")
        mask[5, 5] = 1
        buffered = apply_raster_buffer(mask, 100, 100)
        # Buffer radius 1px should create a 3x3 cross/disk around (5,5)
        assert buffered[5, 5] == 1
        assert buffered[4, 5] == 1
        assert buffered[5, 4] == 1
        assert buffered.sum() > 1

    def test_apply_raster_buffer_zero_distance(self):
        mask = np.zeros((10, 10), dtype="uint8")
        mask[5, 5] = 1
        buffered = apply_raster_buffer(mask, 0, 100)
        assert buffered.sum() == 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Protected Area Exclusions
# ══════════════════════════════════════════════════════════════════════════════


class TestProtectedAreaExclusions:
    """Test per-tier protected area exclusion logic."""

    def test_all_tiers_enabled(self, protected_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"protected_tiers": {"tier1": True, "tier2": True, "tier3": True, "tier4": True}}
        result = apply_protected_area_exclusions(exclusion, protected_tif, config)
        # tier1 (25px) + tier2 (1px) + tier4 (10px) = 36 unique pixels
        # (tier3 is empty)
        assert result.sum() == 36

    def test_tier1_only(self, protected_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"protected_tiers": {"tier1": True, "tier2": False, "tier3": False, "tier4": False}}
        result = apply_protected_area_exclusions(exclusion, protected_tif, config)
        assert result.sum() == 25  # 5x5 quadrant

    def test_all_tiers_disabled(self, protected_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"protected_tiers": {"tier1": False, "tier2": False, "tier3": False, "tier4": False}}
        result = apply_protected_area_exclusions(exclusion, protected_tif, config)
        assert result.sum() == 0

    def test_empty_config_defaults_to_enabled(self, protected_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {}  # No protected_tiers key
        result = apply_protected_area_exclusions(exclusion, protected_tif, config)
        assert result.sum() == 36  # All tiers default to enabled


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Airfield FRZ Exclusions
# ══════════════════════════════════════════════════════════════════════════════


class TestAirfieldExclusions:
    """Test airfield FRZ exclusion logic."""

    def test_enabled(self, airfields_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"airfield_frz": {"enabled": True}}
        result = apply_airfield_exclusions(exclusion, airfields_tif, config)
        assert result.sum() == 20  # 2 rows x 10 cols

    def test_disabled(self, airfields_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"airfield_frz": {"enabled": False}}
        result = apply_airfield_exclusions(exclusion, airfields_tif, config)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Land Cover Exclusions
# ══════════════════════════════════════════════════════════════════════════════


class TestLandCoverExclusions:
    """Test land cover code-based exclusion logic."""

    def test_single_code_exclusion(self, land_cover_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"land_cover": {"exclusion_codes": [20]}}
        result = apply_land_cover_exclusions(exclusion, land_cover_tif, config, RESOLUTION)
        assert result.sum() == 10  # Urban column (10 pixels)

    def test_multiple_codes(self, land_cover_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"land_cover": {"exclusion_codes": [20, 14]}}
        result = apply_land_cover_exclusions(exclusion, land_cover_tif, config, RESOLUTION)
        assert result.sum() == 11  # 10 urban + 1 freshwater

    def test_no_codes_configured(self, land_cover_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"land_cover": {"exclusion_codes": []}}
        result = apply_land_cover_exclusions(exclusion, land_cover_tif, config, RESOLUTION)
        assert result.sum() == 0

    def test_empty_config(self, land_cover_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {}
        result = apply_land_cover_exclusions(exclusion, land_cover_tif, config, RESOLUTION)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Flooding Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestFloodingExclusion:
    """Test flooding risk exclusion logic."""

    def test_enabled(self, flooding_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"flood_zones_enabled": True}
        result = apply_flooding_exclusion(exclusion, flooding_tif, config)
        assert result.sum() == 9  # 3x3 corner

    def test_disabled(self, flooding_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"flood_zones_enabled": False}
        result = apply_flooding_exclusion(exclusion, flooding_tif, config)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Groundwater SPZ Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestGroundwaterExclusion:
    """Test groundwater SPZ exclusion logic."""

    def test_zone_1_only(self, groundwater_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"groundwater_protection": {"exclusion_zones": [1]}}
        result = apply_groundwater_exclusion(exclusion, groundwater_tif, config)
        assert result.sum() == 10  # row 5

    def test_zones_1_and_2(self, groundwater_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"groundwater_protection": {"exclusion_zones": [1, 2]}}
        result = apply_groundwater_exclusion(exclusion, groundwater_tif, config)
        assert result.sum() == 20  # rows 5 and 6

    def test_no_zones_configured(self, groundwater_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {}
        result = apply_groundwater_exclusion(exclusion, groundwater_tif, config)
        assert result.sum() == 0

    def test_single_int_normalised_to_list(self, groundwater_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"groundwater_protection": {"exclusion_zones": 1}}
        result = apply_groundwater_exclusion(exclusion, groundwater_tif, config)
        assert result.sum() == 10


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Coastal Change Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestCoastalChangeExclusion:
    """Test coastal change exclusion with optional buffer."""

    def test_no_buffer(self, coastal_erosion_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"coastal_change_buffer": 0}
        result = apply_coastal_change_exclusion(exclusion, coastal_erosion_tif, config, RESOLUTION)
        assert result.sum() == 2

    def test_with_buffer(self, coastal_erosion_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"coastal_change_buffer": 100}  # 1px buffer
        result = apply_coastal_change_exclusion(exclusion, coastal_erosion_tif, config, RESOLUTION)
        assert result.sum() > 2  # Buffer expands beyond the 2 source pixels


# ══════════════════════════════════════════════════════════════════════════════
# TEST: ALC BMV Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestAlcBmvExclusion:
    """Test ALC BMV agricultural land exclusion."""

    def test_enabled(self, alc_bmv_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"alc_bmv": {"enabled": True}}
        result = apply_alc_bmv_exclusion(exclusion, alc_bmv_tif, config)
        assert result.sum() == 50  # right half

    def test_disabled(self, alc_bmv_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"alc_bmv": {"enabled": False}}
        result = apply_alc_bmv_exclusion(exclusion, alc_bmv_tif, config)
        assert result.sum() == 0

    def test_default_is_disabled(self, alc_bmv_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {}
        result = apply_alc_bmv_exclusion(exclusion, alc_bmv_tif, config)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Green Belt Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestGreenBeltExclusion:
    """Test Green Belt exclusion."""

    def test_enabled(self, green_belt_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"green_belt": {"enabled": True}}
        result = apply_green_belt_exclusion(exclusion, green_belt_tif, config)
        assert result.sum() == 16  # 4x4 centre block

    def test_disabled(self, green_belt_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"green_belt": {"enabled": False}}
        result = apply_green_belt_exclusion(exclusion, green_belt_tif, config)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Scotland Ban Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestScotlandExclusion:
    """Test Scotland ban exclusion."""

    def test_ban_enabled(self, scotland_mask_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        result = apply_scotland_exclusion(exclusion, scotland_mask_tif, scotland_ban=True)
        assert result.sum() == 50  # top half

    def test_ban_disabled(self, scotland_mask_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        result = apply_scotland_exclusion(exclusion, scotland_mask_tif, scotland_ban=False)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Population Criterion Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestPopCriterionExclusion:
    """Test ONR population criterion exclusion."""

    def test_enabled(self, pop_criterion_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"pop_density_criterion": {"enabled": True}}
        result = apply_pop_criterion_exclusion(exclusion, pop_criterion_tif, config)
        assert result.sum() == 50  # left half ineligible

    def test_disabled(self, pop_criterion_tif):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"pop_density_criterion": {"enabled": False}}
        result = apply_pop_criterion_exclusion(exclusion, pop_criterion_tif, config)
        assert result.sum() == 0

    def test_nodata_excluded(self, tmp_path):
        """Nodata pixels (255) should be excluded too."""
        data = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype="uint8")
        data[0, :] = 255  # nodata row
        data[1, :] = 1    # ineligible row
        path = tmp_path / "pop_nodata.tif"
        _write_uint8_tif(path, data)

        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"pop_density_criterion": {"enabled": True}}
        result = apply_pop_criterion_exclusion(exclusion, str(path), config)
        assert result.sum() == 20  # nodata row + ineligible row


# ══════════════════════════════════════════════════════════════════════════════
# TEST: COMAH Buffer Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestComahExclusion:
    """Test COMAH Upper Tier site rasterization and exclusion."""

    def test_enabled_excludes_pixels(self, comah_csv, template):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"comah": {"enabled": True, "buffer": 200}}
        result = apply_comah_exclusion(exclusion, comah_csv, config, template, RESOLUTION)
        # Site A at (400500, 300500) is inside the grid, should exclude pixels
        assert result.sum() > 0

    def test_disabled(self, comah_csv, template):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"comah": {"enabled": False}}
        result = apply_comah_exclusion(exclusion, comah_csv, config, template, RESOLUTION)
        assert result.sum() == 0

    def test_site_outside_grid_no_exclusion(self, tmp_path, template):
        """A COMAH site entirely outside the raster grid should exclude nothing."""
        df = pd.DataFrame({
            "operator_name": ["Far Away"],
            "postcode_easting": [600_000.0],
            "postcode_northing": [600_000.0],
        })
        path = tmp_path / "comah_far.csv"
        df.to_csv(path, index=False)

        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"comah": {"enabled": True, "buffer": 100}}
        result = apply_comah_exclusion(exclusion, str(path), config, template, RESOLUTION)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Gas Pipe Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestGasPipeExclusion:
    """Test gas pipeline rasterization and exclusion."""

    def test_enabled_excludes_pixels(self, gas_pipe_shp, template):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"gas_pipe": {"enabled": True, "buffer": 100}}
        result = apply_gas_pipe_exclusion(exclusion, gas_pipe_shp, config, template, RESOLUTION)
        assert result.sum() > 0

    def test_disabled(self, gas_pipe_shp, template):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"gas_pipe": {"enabled": False}}
        result = apply_gas_pipe_exclusion(exclusion, gas_pipe_shp, config, template, RESOLUTION)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Water Availability Constraint
# ══════════════════════════════════════════════════════════════════════════════


class TestWaterConstraint:
    """Test water availability placeholder."""

    def test_disabled_passes_all(self):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"water_constraint": {"enabled": False}}
        result = apply_water_constraint(exclusion, config)
        assert result.sum() == 0

    def test_empty_config_defaults_disabled(self):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {}
        result = apply_water_constraint(exclusion, config)
        assert result.sum() == 0

    def test_enabled_raises_not_implemented(self):
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"water_constraint": {"enabled": True}}
        with pytest.raises(NotImplementedError, match="Water constraint is enabled"):
            apply_water_constraint(exclusion, config)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Exclusion Mask Accumulation (OR logic)
# ══════════════════════════════════════════════════════════════════════════════


class TestExclusionAccumulation:
    """Test that exclusion layers accumulate via logical OR."""

    def test_or_logic_two_layers(self, protected_tif, flooding_tif):
        """Protected + flooding exclusions should OR together, not overwrite."""
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        config = {"protected_tiers": {"tier1": True, "tier2": False, "tier3": False, "tier4": False}}
        exclusion = apply_protected_area_exclusions(exclusion, protected_tif, config)
        protected_count = exclusion.sum()

        config_flood = {"flood_zones_enabled": True}
        exclusion = apply_flooding_exclusion(exclusion, flooding_tif, config_flood)
        combined_count = exclusion.sum()

        # Combined should be >= each individual (OR logic)
        assert combined_count >= protected_count
        assert combined_count >= 9  # flooding alone
        # Specifically: 25 (tier1) + 9 (flooding) = 34 (no overlap)
        assert combined_count == 34

    def test_overlapping_layers_dedup(self, scotland_mask_tif, pop_criterion_tif):
        """Two overlapping layers should not double-count pixels."""
        exclusion = np.zeros((GRID_HEIGHT, GRID_WIDTH), dtype=bool)
        exclusion = apply_scotland_exclusion(exclusion, scotland_mask_tif, scotland_ban=True)
        scotland_count = exclusion.sum()  # 50

        config = {"pop_density_criterion": {"enabled": True}}
        exclusion = apply_pop_criterion_exclusion(exclusion, pop_criterion_tif, config)
        combined_count = exclusion.sum()

        # Scotland (top 5 rows) + pop ineligible (left 5 cols)
        # Overlap: top-left 5x5 = 25 pixels
        # Union: 50 + 50 - 25 = 75
        assert combined_count == 75


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Full Pipeline Integration
# ══════════════════════════════════════════════════════════════════════════════


class TestFullPipeline:
    """Test the full build_smr_exclusion function."""

    @pytest.fixture
    def zones_geojson(self, tmp_path):
        """Two zones covering the test grid: north half and south half."""
        zone_north = box(XMIN, 300_500, XMAX, YMAX)
        zone_south = box(XMIN, YMIN, XMAX, 300_500)
        gdf = gpd.GeoDataFrame(
            {"zone_name": ["North", "South"]},
            geometry=[zone_north, zone_south],
            crs=TEST_CRS,
        )
        path = tmp_path / "zones.geojson"
        gdf.to_file(path, driver="GeoJSON")
        return str(path)

    @pytest.fixture
    def all_input_paths(
        self, protected_tif, airfields_tif, land_cover_tif, flooding_tif,
        groundwater_tif, coastal_erosion_tif, alc_bmv_tif, green_belt_tif,
        scotland_mask_tif, pop_criterion_tif, comah_csv, gas_pipe_shp,
        zones_geojson,
    ):
        return {
            "protected": protected_tif,
            "airfields": airfields_tif,
            "land_cover": land_cover_tif,
            "flooding": flooding_tif,
            "groundwater": groundwater_tif,
            "coastal_erosion": coastal_erosion_tif,
            "alc_bmv": alc_bmv_tif,
            "green_belt": green_belt_tif,
            "scotland_mask": scotland_mask_tif,
            "pop_criterion": pop_criterion_tif,
            "comah": comah_csv,
            "gas_pipe": gas_pipe_shp,
            "zones": zones_geojson,
        }

    def test_returns_exclusion_and_template(self, all_input_paths):
        from scripts.land.build_nuclear_smr_exclusions import build_smr_exclusion

        smr_config = {
            "protected_tiers": {"tier1": True, "tier2": False, "tier3": False, "tier4": False},
            "airfield_frz": {"enabled": False},
            "land_cover": {"exclusion_codes": [20]},
            "flood_zones_enabled": True,
            "groundwater_protection": {"exclusion_zones": [1]},
            "coastal_change_buffer": 0,
            "alc_bmv": {"enabled": False},
            "green_belt": {"enabled": False},
            "pop_density_criterion": {"enabled": True},
            "comah": {"enabled": False},
            "gas_pipe": {"enabled": False},
            "water_constraint": {"enabled": False},
        }
        exclusion, template = build_smr_exclusion(
            input_paths=all_input_paths,
            smr_config=smr_config,
            scotland_ban=True,
            resolution_m=RESOLUTION,
        )
        assert exclusion.shape == (GRID_HEIGHT, GRID_WIDTH)
        assert exclusion.dtype == bool
        assert len(template) == 4
        assert exclusion.sum() > 0

    def test_scotland_fully_excluded(self, all_input_paths):
        """All Scotland pixels (top half) must be excluded."""
        from scripts.land.build_nuclear_smr_exclusions import build_smr_exclusion

        # Minimal config — only Scotland ban
        smr_config = {
            "protected_tiers": {"tier1": False, "tier2": False, "tier3": False, "tier4": False},
            "airfield_frz": {"enabled": False},
            "land_cover": {"exclusion_codes": []},
            "flood_zones_enabled": False,
            "groundwater_protection": {},
            "coastal_change_buffer": 0,
            "alc_bmv": {"enabled": False},
            "green_belt": {"enabled": False},
            "pop_density_criterion": {"enabled": False},
            "comah": {"enabled": False},
            "gas_pipe": {"enabled": False},
            "water_constraint": {"enabled": False},
        }
        exclusion, _ = build_smr_exclusion(
            input_paths=all_input_paths,
            smr_config=smr_config,
            scotland_ban=True,
            resolution_m=RESOLUTION,
        )
        # Top 5 rows should all be excluded (Scotland)
        assert exclusion[:5, :].all()
        # Bottom 5 rows should NOT all be excluded
        assert not exclusion[5:, :].all()

    def test_all_layers_disabled_only_scotland(self, all_input_paths):
        """With all layers disabled except Scotland ban, only Scotland is excluded."""
        from scripts.land.build_nuclear_smr_exclusions import build_smr_exclusion

        smr_config = {
            "protected_tiers": {"tier1": False, "tier2": False, "tier3": False, "tier4": False},
            "airfield_frz": {"enabled": False},
            "land_cover": {"exclusion_codes": []},
            "flood_zones_enabled": False,
            "groundwater_protection": {},
            "coastal_change_buffer": 0,
            "alc_bmv": {"enabled": False},
            "green_belt": {"enabled": False},
            "pop_density_criterion": {"enabled": False},
            "comah": {"enabled": False},
            "gas_pipe": {"enabled": False},
            "water_constraint": {"enabled": False},
        }
        exclusion, _ = build_smr_exclusion(
            input_paths=all_input_paths,
            smr_config=smr_config,
            scotland_ban=True,
            resolution_m=RESOLUTION,
        )
        assert exclusion.sum() == 50  # exactly Scotland (top half)
