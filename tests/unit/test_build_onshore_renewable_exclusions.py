"""
Unit tests for build_onshore_renewable_exclusions.py

Tests the onshore renewable availability calculation pipeline:
- Buffer distance to pixel conversion
- Raster buffer application (morphological dilation)
- Protected area exclusions (4-band, per-tier buffers, Scotland split)
- FRZ exclusion zones (2-band, pre-buffered — all technologies)
- Land cover exclusions (code masks + per-code buffers)
- Flooding risk exclusion (hard exclusion)
- Groundwater SPZ exclusion (band selection per config)
- Full technology exclusion pipeline (onwind vs solar)
- End-to-end availability fraction calculation
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_onshore_renewable_exclusions import (
    apply_airfield_exclusions,
    apply_flooding_exclusion,
    apply_groundwater_exclusion,
    apply_land_cover_exclusions,
    apply_protected_area_exclusions,
    apply_raster_buffer,
    buffer_distance_to_pixels,
    build_technology_exclusion,
)
from scripts.utilities.land_utils import (
    calculate_zone_fraction,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test rasters (1km x 1km in EPSG:27700)."""
    return (400_000, 300_000, 401_000, 301_000)


@pytest.fixture
def small_shape():
    """Raster shape (height, width) for 1km x 1km at 100m."""
    return (10, 10)


@pytest.fixture
def small_transform(small_bounds, small_shape):
    """Affine transform for 100m resolution raster."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    return from_bounds(xmin, ymin, xmax, ymax, width, height)


@pytest.fixture
def template(small_shape, small_transform):
    """Reference grid template: (width, height, transform, CRS)."""
    height, width = small_shape
    return (width, height, small_transform, "EPSG:27700")


@pytest.fixture
def protected_raster(tmp_path, small_bounds, small_shape):
    """
    4-band protected areas raster. Each tier has pixels in distinct locations.

    Band 1 (Tier 1): pixel at (0, 0) — top-left
    Band 2 (Tier 2): pixel at (0, 9) — top-right
    Band 3 (Tier 3): pixel at (9, 0) — bottom-left
    Band 4 (Tier 4): pixel at (9, 9) — bottom-right
    """
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((4, height, width), dtype="uint8")
    data[0, 0, 0] = 1  # Tier 1
    data[1, 0, 9] = 1  # Tier 2
    data[2, 9, 0] = 1  # Tier 3
    data[3, 9, 9] = 1  # Tier 4

    path = tmp_path / "protected_areas_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 4,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def airfields_raster(tmp_path, small_bounds, small_shape):
    """
    2-band airfields raster.

    Band 1 (MoD): pixel at (2, 2)
    Band 2 (Civilian): pixel at (2, 7)
    """
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((2, height, width), dtype="uint8")
    data[0, 2, 2] = 1  # MoD
    data[1, 2, 7] = 1  # Civilian

    path = tmp_path / "airfields_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 2,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def land_cover_raster(tmp_path, small_bounds, small_shape):
    """
    Single-band land cover raster with LCM 2024 codes.

    Row 0: Urban (20)
    Row 1: Suburban (21)
    Row 2: Arable (3)
    Rows 3-9: Improved Grassland (4) — not excluded
    """
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.full((1, height, width), 4, dtype="uint8")  # Grassland default
    data[0, 0, :] = 20  # Urban row
    data[0, 1, :] = 21  # Suburban row
    data[0, 2, :] = 3   # Arable row

    path = tmp_path / "land_cover_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def flooding_raster(tmp_path, small_bounds, small_shape):
    """Single-band flooding raster. Bottom row is flood zone."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((1, height, width), dtype="uint8")
    data[0, 9, :] = 1  # Bottom row = flood zone

    path = tmp_path / "flooding_risk_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def groundwater_raster(tmp_path, small_bounds, small_shape):
    """
    2-band groundwater raster.

    Band 1 (SPZ1): pixel at (5, 5)
    Band 2 (SPZ 2/3): pixels at (6, 5) and (7, 5)
    """
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((2, height, width), dtype="uint8")
    data[0, 5, 5] = 1  # SPZ1
    data[1, 6, 5] = 1  # SPZ 2/3
    data[1, 7, 5] = 1

    path = tmp_path / "groundwater_spz_ew.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 2,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def coastal_erosion_raster(tmp_path, small_bounds, small_shape):
    """Single-band coastal erosion raster (all zeros)."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((1, height, width), dtype="uint8")
    path = tmp_path / "coastal_erosion_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def alc_bmv_raster(tmp_path, small_bounds, small_shape):
    """Single-band ALC BMV raster (all zeros)."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((1, height, width), dtype="uint8")
    path = tmp_path / "alc_bmv_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def green_belt_raster(tmp_path, small_bounds, small_shape):
    """Single-band Green Belt raster (all zeros)."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((1, height, width), dtype="uint8")
    path = tmp_path / "green_belt_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
    return str(path)


@pytest.fixture
def scotland_mask(small_shape):
    """Scotland mask: left half is Scotland (1), right half is E&W (0)."""
    height, width = small_shape
    mask = np.zeros((height, width), dtype=bool)
    mask[:, :5] = True  # Left 5 columns = Scotland
    return mask


@pytest.fixture
def scotland_mask_raster(tmp_path, small_bounds, scotland_mask):
    """Scotland mask as GeoTIFF."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = scotland_mask.shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    path = tmp_path / "scotland_mask_Zonal.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(scotland_mask.astype("uint8"), 1)

    return str(path)


@pytest.fixture
def zones_gdf(small_bounds):
    """Two zones: left half (Zone_A) and right half (Zone_B)."""
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2

    return gpd.GeoDataFrame(
        {
            "zone_name": ["Zone_A", "Zone_B"],
            "geometry": [
                box(xmin, ymin, mid_x, ymax),
                box(mid_x, ymin, xmax, ymax),
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def zones_file(tmp_path, zones_gdf):
    """Write zones to GeoJSON."""
    path = tmp_path / "zones.geojson"
    zones_gdf.rename(columns={"zone_name": "Name_1"}).to_file(path, driver="GeoJSON")
    return str(path)


@pytest.fixture
def all_input_paths(
    protected_raster,
    airfields_raster,
    land_cover_raster,
    flooding_raster,
    groundwater_raster,
    coastal_erosion_raster,
    alc_bmv_raster,
    green_belt_raster,
    scotland_mask_raster,
    zones_file,
):
    """All input paths dict matching main() expectations."""
    return {
        "protected": protected_raster,
        "airfields": airfields_raster,
        "land_cover": land_cover_raster,
        "flooding": flooding_raster,
        "groundwater": groundwater_raster,
        "coastal_erosion": coastal_erosion_raster,
        "alc_bmv": alc_bmv_raster,
        "green_belt": green_belt_raster,
        "scotland_mask": scotland_mask_raster,
        "zones": zones_file,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Buffer Distance to Pixels
# ══════════════════════════════════════════════════════════════════════════════


class TestBufferDistanceToPixels:
    """Test buffer distance conversion."""

    def test_exact_multiple(self):
        assert buffer_distance_to_pixels(500, 100) == 5

    def test_rounds_up(self):
        assert buffer_distance_to_pixels(550, 100) == 6

    def test_rounds_down(self):
        assert buffer_distance_to_pixels(440, 100) == 4

    def test_minimum_one_pixel(self):
        assert buffer_distance_to_pixels(10, 100) == 1

    def test_zero_returns_zero(self):
        assert buffer_distance_to_pixels(0, 100) == 0

    def test_negative_returns_zero(self):
        assert buffer_distance_to_pixels(-100, 100) == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Apply Raster Buffer
# ══════════════════════════════════════════════════════════════════════════════


class TestApplyRasterBuffer:
    """Test morphological dilation buffering."""

    def test_zero_buffer_returns_mask(self):
        mask = np.zeros((10, 10), dtype="uint8")
        mask[5, 5] = 1
        result = apply_raster_buffer(mask, 0, 100)
        np.testing.assert_array_equal(result, mask)

    def test_buffer_expands_single_pixel(self):
        mask = np.zeros((10, 10), dtype="uint8")
        mask[5, 5] = 1
        result = apply_raster_buffer(mask, 100, 100)
        # 1-pixel radius disk should expand to at least 5 pixels
        assert result.sum() > 1
        assert result[5, 5] == 1  # Centre preserved
        assert result[4, 5] == 1  # Adjacent pixels included
        assert result[5, 4] == 1

    def test_buffer_returns_uint8(self):
        mask = np.zeros((10, 10), dtype="uint8")
        mask[5, 5] = 1
        result = apply_raster_buffer(mask, 200, 100)
        assert result.dtype == np.uint8

    def test_empty_mask_stays_empty(self):
        mask = np.zeros((10, 10), dtype="uint8")
        result = apply_raster_buffer(mask, 500, 100)
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Protected Area Exclusions
# ══════════════════════════════════════════════════════════════════════════════


class TestProtectedAreaExclusions:
    """Test protected area exclusion application."""

    def test_tier4_hard_exclusion_no_config(self, protected_raster):
        """Tier 4 (not in config) should default to enabled (hard exclusion)."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {"protected_tiers": {"tier1": False, "tier2": False, "tier3": False}}
        result = apply_protected_area_exclusions(
            exclusion, protected_raster, tech_config
        )
        assert result[9, 9]  # Tier 4 pixel excluded (defaults to True)

    def test_tier_disabled_skipped(self, protected_raster):
        """Tier set to False should be skipped."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {"protected_tiers": {"tier1": False}}
        result = apply_protected_area_exclusions(
            exclusion, protected_raster, tech_config
        )
        assert not result[0, 0]  # Tier 1 pixel not excluded

    def test_tier_enabled_excluded(self, protected_raster):
        """Tier set to True should be hard exclusion."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {"protected_tiers": {"tier1": True}}
        result = apply_protected_area_exclusions(
            exclusion, protected_raster, tech_config
        )
        assert result[0, 0]  # Tier 1 pixel excluded

    def test_no_config_all_hard_exclusion(self, protected_raster):
        """No protected_tiers config → all tiers default to enabled."""
        exclusion = np.zeros((10, 10), dtype=bool)
        result = apply_protected_area_exclusions(
            exclusion, protected_raster, {}
        )
        # All 4 tier pixels should be excluded
        assert result[0, 0]   # Tier 1
        assert result[0, 9]   # Tier 2
        assert result[9, 0]   # Tier 3
        assert result[9, 9]   # Tier 4
        assert result.sum() == 4  # Only the source pixels, no buffer


# ══════════════════════════════════════════════════════════════════════════════
# TEST: FRZ Exclusion Zones
# ══════════════════════════════════════════════════════════════════════════════


class TestAirfieldExclusions:
    """Test FRZ exclusion zone application (pre-buffered raster)."""

    def test_both_bands_excluded(self, airfields_raster):
        """Both MoD and civilian FRZ pixels should be excluded."""
        exclusion = np.zeros((10, 10), dtype=bool)
        result = apply_airfield_exclusions(exclusion, airfields_raster, {})
        assert result[2, 2]  # MoD FRZ pixel
        assert result[2, 7]  # Civilian FRZ pixel

    def test_no_buffer_expansion(self, airfields_raster):
        """FRZ exclusion is hard — no buffer expansion beyond raster pixels."""
        exclusion = np.zeros((10, 10), dtype=bool)
        result = apply_airfield_exclusions(exclusion, airfields_raster, {})
        assert result.sum() == 2  # Exactly the 2 source pixels

    def test_preserves_existing_exclusion(self, airfields_raster):
        """FRZ exclusion should OR with existing exclusion mask."""
        exclusion = np.zeros((10, 10), dtype=bool)
        exclusion[5, 5] = True  # Pre-existing exclusion
        result = apply_airfield_exclusions(exclusion, airfields_raster, {})
        assert result[5, 5]  # Pre-existing preserved
        assert result[2, 2]  # MoD added
        assert result.sum() == 3


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Land Cover Exclusions
# ══════════════════════════════════════════════════════════════════════════════


class TestLandCoverExclusions:
    """Test land cover exclusion application."""

    def test_hard_exclusion_codes(self, land_cover_raster):
        """Excluded codes without buffers should be hard exclusion."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {
            "land_cover": {
                "exclusion_codes": [20, 21],
                "buffer_distances": {},
            }
        }
        result = apply_land_cover_exclusions(
            exclusion, land_cover_raster, tech_config, 100
        )
        # Row 0 (Urban=20) and Row 1 (Suburban=21) excluded
        assert result[0, :].all()  # All Urban pixels
        assert result[1, :].all()  # All Suburban pixels
        assert not result[3, :].any()  # Grassland not excluded

    def test_buffer_on_specific_code(self, land_cover_raster):
        """Code with buffer distance should expand exclusion."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {
            "land_cover": {
                "exclusion_codes": [20],
                "buffer_distances": {20: 200},
            }
        }
        result = apply_land_cover_exclusions(
            exclusion, land_cover_raster, tech_config, 100
        )
        # Urban row 0 with 200m buffer should expand to rows 0-2
        assert result[0, :].all()
        assert result.sum() > 10  # More than just the source row

    def test_solar_excludes_arable(self, land_cover_raster):
        """Solar config includes code 3 (Arable)."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {
            "land_cover": {
                "exclusion_codes": [3, 20, 21],
                "buffer_distances": {},
            }
        }
        result = apply_land_cover_exclusions(
            exclusion, land_cover_raster, tech_config, 100
        )
        assert result[2, :].all()  # Arable row excluded

    def test_no_config_skips(self, land_cover_raster):
        """No land_cover config should skip entirely."""
        exclusion = np.zeros((10, 10), dtype=bool)
        result = apply_land_cover_exclusions(
            exclusion, land_cover_raster, {}, 100
        )
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Flooding Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestFloodingExclusion:
    """Test flooding risk exclusion."""

    def test_flood_zone_excluded(self, flooding_raster):
        """Bottom row flood zone should be excluded."""
        exclusion = np.zeros((10, 10), dtype=bool)
        result = apply_flooding_exclusion(exclusion, flooding_raster, {})
        assert result[9, :].all()  # Bottom row
        assert not result[0, :].any()  # Top row not excluded
        assert result.sum() == 10  # Only bottom row


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Groundwater Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestGroundwaterExclusion:
    """Test groundwater SPZ exclusion."""

    def test_spz1_only(self, groundwater_raster):
        """Config exclusion_zones=1 should exclude Band 1 only."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {"groundwater_protection": {"exclusion_zones": 1}}
        result = apply_groundwater_exclusion(
            exclusion, groundwater_raster, tech_config
        )
        assert result[5, 5]  # SPZ1 pixel
        assert not result[6, 5]  # SPZ 2/3 pixel NOT excluded
        assert result.sum() == 1

    def test_spz_all_zones(self, groundwater_raster):
        """Config exclusion_zones=[1,2,3] should exclude Bands 1 and 2."""
        exclusion = np.zeros((10, 10), dtype=bool)
        tech_config = {"groundwater_protection": {"exclusion_zones": [1, 2, 3]}}
        result = apply_groundwater_exclusion(
            exclusion, groundwater_raster, tech_config
        )
        assert result[5, 5]  # SPZ1
        assert result[6, 5]  # SPZ 2/3
        assert result[7, 5]  # SPZ 2/3
        assert result.sum() == 3

    def test_no_config_skips(self, groundwater_raster):
        """No groundwater config should skip."""
        exclusion = np.zeros((10, 10), dtype=bool)
        result = apply_groundwater_exclusion(exclusion, groundwater_raster, {})
        assert result.sum() == 0


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Technology Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildTechnologyExclusion:
    """Test full technology exclusion pipeline."""

    def test_onwind_includes_airfield_exclusion(self, all_input_paths, template):
        """Onwind should apply FRZ exclusion zones."""
        tech_config = {
            "protected_buffers": {"tier1": 0},
            "land_cover": {"exclusion_codes": [20], "buffer_distances": {}},
            "groundwater_protection": {"exclusion_zones": 1},
        }
        result = build_technology_exclusion(
            "onwind", tech_config, all_input_paths, template, 100
        )
        assert result.dtype == bool
        assert result.shape == (10, 10)
        # FRZ pixels should be excluded
        assert result[2, 2]  # MoD FRZ pixel
        assert result[2, 7]  # Civilian FRZ pixel

    def test_solar_also_excludes_airfields(self, all_input_paths, template):
        """Solar should also apply FRZ exclusion (all technologies)."""
        tech_config = {
            "protected_buffers": {"tier1": 0},
            "land_cover": {"exclusion_codes": [3, 20, 21], "buffer_distances": {}},
            "groundwater_protection": {"exclusion_zones": 1},
        }
        result = build_technology_exclusion(
            "solar", tech_config, all_input_paths, template, 100
        )
        assert result.dtype == bool
        # FRZ pixel at (2,2) should be excluded for solar too
        assert result[2, 2]  # MoD FRZ pixel excluded

    def test_solar_has_more_land_cover_exclusions(
        self, all_input_paths, template
    ):
        """Solar excludes arable (code 3) which onwind does not."""
        onwind_config = {
            "land_cover": {"exclusion_codes": [20, 21], "buffer_distances": {}},
        }
        solar_config = {
            "land_cover": {"exclusion_codes": [3, 20, 21], "buffer_distances": {}},
        }
        onwind_result = build_technology_exclusion(
            "onwind", onwind_config, all_input_paths, template, 100
        )
        solar_result = build_technology_exclusion(
            "solar", solar_config, all_input_paths, template, 100
        )
        # Solar should exclude more (arable row)
        assert solar_result.sum() > onwind_result.sum()


# ══════════════════════════════════════════════════════════════════════════════
# TEST: End-to-End Pipeline
# ══════════════════════════════════════════════════════════════════════════════


class TestEndToEnd:
    """Integration tests for the full availability calculation."""

    def test_availability_in_valid_range(
        self, all_input_paths, template, zones_gdf, small_transform
    ):
        """Availability fractions should be between 0 and 1."""
        tech_config = {
            "land_cover": {"exclusion_codes": [20], "buffer_distances": {}},
        }
        exclusion = build_technology_exclusion(
            "onwind", tech_config, all_input_paths, template, 100
        )
        available = (~exclusion).astype("uint8")
        fractions = calculate_zone_fraction(available, zones_gdf, small_transform)

        assert (fractions >= 0.0).all()
        assert (fractions <= 1.0).all()

    def test_more_exclusion_means_less_availability(
        self, all_input_paths, template, zones_gdf, small_transform
    ):
        """Adding more exclusion layers should reduce availability."""
        # Minimal exclusion
        config_min = {
            "land_cover": {"exclusion_codes": [20], "buffer_distances": {}},
        }
        excl_min = build_technology_exclusion(
            "test", config_min, all_input_paths, template, 100
        )
        avail_min = calculate_zone_fraction(
            (~excl_min).astype("uint8"), zones_gdf, small_transform
        )

        # More exclusion
        config_max = {
            "land_cover": {"exclusion_codes": [3, 20, 21], "buffer_distances": {}},
            "groundwater_protection": {"exclusion_zones": [1, 2, 3]},
        }
        excl_max = build_technology_exclusion(
            "test", config_max, all_input_paths, template, 100
        )
        avail_max = calculate_zone_fraction(
            (~excl_max).astype("uint8"), zones_gdf, small_transform
        )

        assert avail_max.sum() <= avail_min.sum()

    def test_full_exclusion_gives_zero_availability(
        self, all_input_paths, template, zones_gdf, small_transform
    ):
        """Excluding all land cover codes should give near-zero availability."""
        tech_config = {
            "land_cover": {
                "exclusion_codes": list(range(1, 22)),  # All 21 codes
                "buffer_distances": {},
            },
        }
        exclusion = build_technology_exclusion(
            "test", tech_config, all_input_paths, template, 100
        )
        available = (~exclusion).astype("uint8")
        fractions = calculate_zone_fraction(available, zones_gdf, small_transform)

        # Should be very low (nodata pixels might not be excluded)
        assert fractions.sum() < 0.1
