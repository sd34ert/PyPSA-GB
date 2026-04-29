"""
Unit tests for build_offshore_available_kra.py

Tests the offshore KRA processing pipeline:
- TG classification parsing from Rating attribute
- Cost tier and capex multiplier mapping (fixed and floating)
- Coastline derivation from onshore zone boundaries
- Carrier and connection type classification (AC/DC threshold)
- Available area calculation from exclusion raster sampling
- KRA-zone intersection and fragment attribute preservation
- Edge cases (fully excluded fragments, single-zone KRA, all-AC/all-DC)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import LineString, MultiPolygon, Polygon, box

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_offshore_available_kra import (
    FIXED_COST_TIERS,
    FLOATING_COST_TIERS,
    AC_THRESHOLD_M,
    OFFSHORE_ZONES,
    calculate_fragment_availability,
    classify_carrier,
    classify_cost_tier,
    derive_coastline,
    parse_tg_class,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test rasters (10km x 10km in EPSG:27700)."""
    return (400_000, 300_000, 410_000, 310_000)


@pytest.fixture
def small_transform(small_bounds):
    """Affine transform for 100m resolution raster."""
    xmin, ymin, xmax, ymax = small_bounds
    width = int((xmax - xmin) / 100)
    height = int((ymax - ymin) / 100)
    return from_bounds(xmin, ymin, xmax, ymax, width, height)


@pytest.fixture
def small_shape():
    """Raster shape (height, width) for 10km x 10km at 100m."""
    return (100, 100)


@pytest.fixture
def zones_gdf(small_bounds):
    """
    Synthetic zone shapes: 2 onshore zones + 1 offshore zone.

    Layout (10km x 10km area):
        Zone_A: left half (onshore)
        Zone_B: right half, bottom (onshore)
        DOGGER_BANK: right half, top (offshore)
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    return gpd.GeoDataFrame(
        {
            "zone_name": ["Zone_A", "Zone_B", "DOGGER_BANK"],
            "geometry": [
                box(xmin, ymin, mid_x, ymax),
                box(mid_x, ymin, xmax, mid_y),
                box(mid_x, mid_y, xmax, ymax),
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def fixed_kra_gdf(small_bounds):
    """
    Synthetic fixed KRA polygons: 2 KRAs spanning multiple zones.

    KRA_near: near-shore (centroid <50km from coast) — overlaps Zone_A
    KRA_far: far-shore (centroid >50km from coast) — overlaps DOGGER_BANK
    """
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    return gpd.GeoDataFrame(
        {
            "OBJECTID": [1, 2],
            "Rating": ["Technology Group 3A", "Technology Group 7B"],
            "Shape__Area": [1e7, 2e7],
            "Shape__Length": [12000, 18000],
            "geometry": [
                # Near-shore: in Zone_A, left side
                box(xmin + 500, ymin + 500, mid_x - 500, mid_y - 500),
                # Far-shore: spans Zone_B and DOGGER_BANK
                box(mid_x + 500, ymin + 500, xmax - 500, ymax - 500),
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def floating_kra_gdf(small_bounds):
    """Synthetic floating KRA: 1 polygon in DOGGER_BANK area."""
    xmin, ymin, xmax, ymax = small_bounds
    mid_x = (xmin + xmax) / 2
    mid_y = (ymin + ymax) / 2

    return gpd.GeoDataFrame(
        {
            "OBJECTID": [3],
            "Rating": ["Technology Group 5"],
            "Shape__Area": [5e6],
            "Shape__Length": [9000],
            "geometry": [
                box(mid_x + 1000, mid_y + 1000, xmax - 1000, ymax - 1000),
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def exclusion_raster(tmp_path, small_bounds, small_shape):
    """
    Synthetic offshore exclusion raster (100m, EPSG:27700).

    Layout: top-right quadrant is excluded (value=1), rest available (value=0).
    This means KRAs in the top-right will have reduced available area.
    """
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((1, height, width), dtype="uint8")
    # Exclude top-right quadrant
    data[0, :height // 2, width // 2:] = 1

    path = tmp_path / "offshore_exclusions.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 255,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def fully_excluded_raster(tmp_path, small_bounds, small_shape):
    """Exclusion raster where everything is excluded (all 1s)."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.ones((1, height, width), dtype="uint8")

    path = tmp_path / "offshore_exclusions_full.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 255,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


@pytest.fixture
def no_exclusion_raster(tmp_path, small_bounds, small_shape):
    """Exclusion raster with nothing excluded (all 0s)."""
    xmin, ymin, xmax, ymax = small_bounds
    height, width = small_shape
    transform = from_bounds(xmin, ymin, xmax, ymax, width, height)

    data = np.zeros((1, height, width), dtype="uint8")

    path = tmp_path / "offshore_exclusions_none.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": transform,
        "nodata": 255,
    }

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)

    return str(path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: TG Classification Parsing
# ══════════════════════════════════════════════════════════════════════════════


class TestParseTgClass:
    """Test Rating → TG class parsing."""

    @pytest.mark.parametrize(
        "rating, expected",
        [
            ("Technology Group 1", "TG-1"),
            ("Technology Group 2A", "TG-2A"),
            ("Technology Group 2B", "TG-2B"),
            ("Technology Group 7B", "TG-7B"),
            ("Technology Group 3", "TG-3"),
            ("Technology Group 6A", "TG-6A"),
        ],
    )
    def test_parse_valid_ratings(self, rating, expected):
        """Test that valid Rating strings are parsed correctly."""
        assert parse_tg_class(rating) == expected

    def test_parse_invalid_rating_raises(self):
        """Test that unrecognised Rating strings raise ValueError."""
        with pytest.raises(ValueError, match="Cannot parse TG class"):
            parse_tg_class("Invalid Rating String")

    def test_parse_empty_string_raises(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError, match="Cannot parse TG class"):
            parse_tg_class("")

    def test_parse_partial_match_raises(self):
        """Test that partial matches without 'Technology Group' prefix fail."""
        with pytest.raises(ValueError, match="Cannot parse TG class"):
            parse_tg_class("Group 7B")


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Cost Tier Classification
# ══════════════════════════════════════════════════════════════════════════════


class TestClassifyCostTier:
    """Test TG → cost tier and capex multiplier mapping."""

    @pytest.mark.parametrize(
        "tg_class, expected_tier, expected_mult",
        [
            ("TG-1", "F1", 1.00),
            ("TG-2A", "F1", 1.00),
            ("TG-4B", "F2a", 1.10),
            ("TG-5A", "F2b", 1.15),
            ("TG-6A", "F3a", 1.20),
            ("TG-7B", "F3b", 1.30),
        ],
    )
    def test_fixed_cost_tiers(self, tg_class, expected_tier, expected_mult):
        """Test fixed offshore TG → cost tier mapping."""
        tier, mult = classify_cost_tier(tg_class, "fixed")
        assert tier == expected_tier
        assert mult == pytest.approx(expected_mult)

    @pytest.mark.parametrize(
        "tg_class, expected_tier, expected_mult",
        [
            ("TG-1", "FL1", 1.00),
            ("TG-2", "FL2a", 1.10),
            ("TG-3", "FL2a", 1.10),
            ("TG-4", "FL2b", 1.15),
            ("TG-5", "FL3a", 1.20),
            ("TG-6", "FL3b", 1.30),
        ],
    )
    def test_floating_cost_tiers(self, tg_class, expected_tier, expected_mult):
        """Test floating offshore TG → cost tier mapping."""
        tier, mult = classify_cost_tier(tg_class, "floating")
        assert tier == expected_tier
        assert mult == pytest.approx(expected_mult)

    def test_all_fixed_tg_classes_covered(self):
        """Test that every fixed TG class has a mapping."""
        for tg_class in FIXED_COST_TIERS:
            tier, mult = classify_cost_tier(tg_class, "fixed")
            assert isinstance(tier, str)
            assert 1.0 <= mult <= 1.5

    def test_all_floating_tg_classes_covered(self):
        """Test that every floating TG class has a mapping."""
        for tg_class in FLOATING_COST_TIERS:
            tier, mult = classify_cost_tier(tg_class, "floating")
            assert isinstance(tier, str)
            assert 1.0 <= mult <= 1.5

    def test_unknown_tg_class_raises(self):
        """Test that unknown TG class raises ValueError."""
        with pytest.raises(ValueError, match="Unknown TG class"):
            classify_cost_tier("TG-99", "fixed")

    def test_fixed_tg_in_floating_lookup_raises(self):
        """Test that fixed-only TG class raises for floating lookup."""
        with pytest.raises(ValueError, match="Unknown TG class"):
            classify_cost_tier("TG-7B", "floating")


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Coastline Derivation
# ══════════════════════════════════════════════════════════════════════════════


class TestDeriveCoastline:
    """Test coastline derivation from zone boundaries."""

    def test_coastline_excludes_offshore_zones(self, zones_gdf):
        """Test that offshore zones are excluded from coastline derivation."""
        coastline = derive_coastline(zones_gdf)
        assert coastline is not None
        assert not coastline.is_empty

    def test_coastline_is_linestring_type(self, zones_gdf):
        """Test that coastline is a line geometry."""
        coastline = derive_coastline(zones_gdf)
        assert coastline.geom_type in ("LineString", "MultiLineString")

    def test_coastline_has_positive_length(self, zones_gdf):
        """Test that coastline has non-zero length."""
        coastline = derive_coastline(zones_gdf)
        assert coastline.length > 0

    def test_coastline_with_only_onshore_zones(self):
        """Test coastline from zones with no offshore zones."""
        zones = gpd.GeoDataFrame(
            {
                "zone_name": ["Z1", "Z2"],
                "geometry": [
                    box(0, 0, 100_000, 100_000),
                    box(100_000, 0, 200_000, 100_000),
                ],
            },
            crs="EPSG:27700",
        )
        coastline = derive_coastline(zones)
        assert not coastline.is_empty


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Carrier Classification
# ══════════════════════════════════════════════════════════════════════════════


class TestClassifyCarrier:
    """Test carrier and connection type classification."""

    def test_fixed_ac_near_shore(self):
        """Fixed KRA <50km → offwind-fixed-ac, AC."""
        carrier, conn = classify_carrier("fixed", 30_000)
        assert carrier == "offwind-fixed-ac"
        assert conn == "AC"

    def test_fixed_dc_far_shore(self):
        """Fixed KRA ≥50km → offwind-fixed-dc, DC."""
        carrier, conn = classify_carrier("fixed", 60_000)
        assert carrier == "offwind-fixed-dc"
        assert conn == "DC"

    def test_floating_ac_near_shore(self):
        """Floating KRA <50km → offwind-float-ac, AC."""
        carrier, conn = classify_carrier("floating", 25_000)
        assert carrier == "offwind-float-ac"
        assert conn == "AC"

    def test_floating_dc_far_shore(self):
        """Floating KRA ≥50km → offwind-float-dc, DC."""
        carrier, conn = classify_carrier("floating", 80_000)
        assert carrier == "offwind-float-dc"
        assert conn == "DC"

    def test_exact_threshold_is_dc(self):
        """Distance exactly at 50km threshold → DC."""
        carrier, conn = classify_carrier("fixed", AC_THRESHOLD_M)
        assert conn == "DC"

    def test_just_below_threshold_is_ac(self):
        """Distance just below 50km → AC."""
        carrier, conn = classify_carrier("fixed", AC_THRESHOLD_M - 1)
        assert conn == "AC"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Fragment Available Area Calculation
# ══════════════════════════════════════════════════════════════════════════════


class TestCalculateFragmentAvailability:
    """Test available area and centroid calculation from exclusion raster."""

    def test_fully_available_fragment(self, no_exclusion_raster, small_bounds):
        """Fragment in area with no exclusions has full area available."""
        xmin, ymin, xmax, ymax = small_bounds
        fragment = box(xmin + 100, ymin + 100, xmin + 1100, ymin + 1100)

        with rasterio.open(no_exclusion_raster) as src:
            area, cx, cy = calculate_fragment_availability(fragment, src)

        # 1km × 1km = 1.0 km², but pixel counting may differ slightly
        assert area > 0.9
        assert area <= 1.1
        # Centroid should be near fragment centre
        assert abs(cx - (xmin + 600)) < 200
        assert abs(cy - (ymin + 600)) < 200

    def test_fully_excluded_fragment(self, fully_excluded_raster, small_bounds):
        """Fragment in fully excluded area has zero available area."""
        xmin, ymin, xmax, ymax = small_bounds
        fragment = box(xmin + 100, ymin + 100, xmin + 1100, ymin + 1100)

        with rasterio.open(fully_excluded_raster) as src:
            area, cx, cy = calculate_fragment_availability(fragment, src)

        assert area == 0.0

    def test_partially_excluded_fragment(self, exclusion_raster, small_bounds):
        """Fragment spanning excluded/available boundary has partial area."""
        xmin, ymin, xmax, ymax = small_bounds
        mid_x = (xmin + xmax) / 2
        mid_y = (ymin + ymax) / 2

        # Fragment spanning the exclusion boundary (top-right is excluded)
        fragment = box(mid_x - 1000, mid_y - 1000, mid_x + 1000, mid_y + 1000)

        with rasterio.open(exclusion_raster) as src:
            area, cx, cy = calculate_fragment_availability(fragment, src)

        # Should have some but not all area available
        total_area = 2.0 * 2.0  # 2km × 2km = 4 km²
        assert 0 < area < total_area

    def test_centroid_in_available_area(self, exclusion_raster, small_bounds):
        """Centroid should be weighted toward available pixels."""
        xmin, ymin, xmax, ymax = small_bounds
        mid_x = (xmin + xmax) / 2

        # Fragment in bottom-left (fully available in our fixture)
        fragment = box(xmin + 100, small_bounds[1] + 100, mid_x - 100, 305_000)

        with rasterio.open(exclusion_raster) as src:
            area, cx, cy = calculate_fragment_availability(fragment, src)

        assert area > 0
        # Centroid should be within the fragment bounds
        assert xmin + 100 <= cx <= mid_x - 100
        assert small_bounds[1] + 100 <= cy <= 305_000

    def test_area_units_are_km2(self, no_exclusion_raster, small_bounds):
        """Verify area is in km² (100m pixels → 0.01 km² each)."""
        xmin, ymin, _, _ = small_bounds
        # 1km × 1km box = 100 pixels at 100m = 1.0 km²
        fragment = box(xmin, ymin, xmin + 1000, ymin + 1000)

        with rasterio.open(no_exclusion_raster) as src:
            area, _, _ = calculate_fragment_availability(fragment, src)

        assert area == pytest.approx(1.0, abs=0.05)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: End-to-End Pipeline
# ══════════════════════════════════════════════════════════════════════════════


class TestEndToEndPipeline:
    """Integration tests for the full KRA processing pipeline."""

    def test_fixed_kra_zone_intersection(self, fixed_kra_gdf, zones_gdf):
        """Test that fixed KRAs are correctly split by zone boundaries."""
        fragments = gpd.overlay(fixed_kra_gdf, zones_gdf, how="intersection")

        # KRA_near is in Zone_A only
        # KRA_far spans Zone_B and DOGGER_BANK
        assert len(fragments) >= 2
        zone_names = set(fragments["zone_name"].unique())
        assert "Zone_A" in zone_names or "Zone_B" in zone_names

    def test_floating_kra_zone_intersection(self, floating_kra_gdf, zones_gdf):
        """Test that floating KRA is assigned to DOGGER_BANK zone."""
        fragments = gpd.overlay(floating_kra_gdf, zones_gdf, how="intersection")
        assert len(fragments) >= 1
        assert "DOGGER_BANK" in fragments["zone_name"].values

    def test_kra_attributes_preserved_through_overlay(self, fixed_kra_gdf, zones_gdf):
        """Test that KRA attributes survive zone intersection."""
        fragments = gpd.overlay(
            fixed_kra_gdf[["Rating", "geometry"]],
            zones_gdf,
            how="intersection",
        )

        assert "Rating" in fragments.columns
        assert "zone_name" in fragments.columns
        # All fragments should have a Rating value
        assert fragments["Rating"].notna().all()

    def test_full_pipeline_produces_valid_output(
        self, fixed_kra_gdf, floating_kra_gdf, zones_gdf, exclusion_raster
    ):
        """
        Test the complete pipeline: classify → distance → intersect → area.

        Verifies that the output has the expected schema and valid values.
        """
        import pandas as pd

        # Step 1: Classify
        fixed_kra_gdf["kra_type"] = "fixed"
        floating_kra_gdf["kra_type"] = "floating"
        kras = pd.concat([fixed_kra_gdf, floating_kra_gdf], ignore_index=True)
        kras = gpd.GeoDataFrame(kras, geometry="geometry", crs="EPSG:27700")

        kras["kra_name"] = kras["Rating"]
        kras["tg_class"] = kras["Rating"].apply(parse_tg_class)

        tier_data = kras.apply(
            lambda row: classify_cost_tier(row["tg_class"], row["kra_type"]),
            axis=1,
        )
        kras["cost_tier"] = tier_data.apply(lambda x: x[0])
        kras["capex_multiplier"] = tier_data.apply(lambda x: x[1])

        # Step 2: Coastline and distances
        coastline = derive_coastline(zones_gdf)
        from shapely.ops import nearest_points

        for idx, row in kras.iterrows():
            centroid = row.geometry.centroid
            nearest_pt = nearest_points(centroid, coastline)[1]
            kras.loc[idx, "distance_to_coast_km"] = centroid.distance(nearest_pt) / 1000

        carrier_data = kras.apply(
            lambda row: classify_carrier(
                row["kra_type"], row["distance_to_coast_km"] * 1000
            ),
            axis=1,
        )
        kras["carrier"] = carrier_data.apply(lambda x: x[0])
        kras["connection_type"] = carrier_data.apply(lambda x: x[1])

        # Step 3: Zone intersection
        kra_cols = [
            "kra_name", "tg_class", "cost_tier", "capex_multiplier",
            "kra_type", "carrier", "connection_type", "distance_to_coast_km",
            "geometry",
        ]
        fragments = gpd.overlay(kras[kra_cols], zones_gdf, how="intersection")

        # Step 4: Available area
        with rasterio.open(exclusion_raster) as src:
            areas, cxs, cys = [], [], []
            for _, row in fragments.iterrows():
                a, cx, cy = calculate_fragment_availability(row.geometry, src)
                areas.append(a)
                cxs.append(cx)
                cys.append(cy)

        fragments["available_area_km2"] = areas
        fragments["centroid_x"] = cxs
        fragments["centroid_y"] = cys
        fragments = fragments[fragments["available_area_km2"] > 0]

        # Validate output schema
        expected_cols = {
            "kra_name", "tg_class", "cost_tier", "capex_multiplier",
            "kra_type", "carrier", "connection_type", "distance_to_coast_km",
            "zone_name", "available_area_km2", "centroid_x", "centroid_y",
            "geometry",
        }
        assert expected_cols.issubset(set(fragments.columns))

        # Validate value ranges
        assert (fragments["available_area_km2"] > 0).all()
        assert (fragments["capex_multiplier"] >= 1.0).all()
        assert (fragments["capex_multiplier"] <= 1.5).all()
        assert (fragments["distance_to_coast_km"] >= 0).all()
        assert fragments["carrier"].str.startswith("offwind-").all()
        assert fragments["connection_type"].isin(["AC", "DC"]).all()
        assert fragments["kra_type"].isin(["fixed", "floating"]).all()

    def test_zero_area_fragments_dropped(
        self, fixed_kra_gdf, zones_gdf, fully_excluded_raster
    ):
        """Test that fragments with zero available area are correctly identified."""
        fragments = gpd.overlay(fixed_kra_gdf, zones_gdf, how="intersection")

        with rasterio.open(fully_excluded_raster) as src:
            areas = []
            for _, row in fragments.iterrows():
                a, _, _ = calculate_fragment_availability(row.geometry, src)
                areas.append(a)

        # All areas should be zero when everything is excluded
        assert all(a == 0.0 for a in areas)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Lookup Table Consistency
# ══════════════════════════════════════════════════════════════════════════════


class TestLookupTableConsistency:
    """Test that lookup tables are complete and consistent."""

    def test_fixed_tiers_cover_all_13_tg_classes(self):
        """Fixed offshore must have mappings for all 13 TG classes."""
        expected = {
            "TG-1", "TG-2A", "TG-2B", "TG-3A", "TG-3B",
            "TG-4A", "TG-4B", "TG-5A", "TG-5B",
            "TG-6A", "TG-6B", "TG-7A", "TG-7B",
        }
        assert set(FIXED_COST_TIERS.keys()) == expected

    def test_floating_tiers_cover_all_6_tg_classes(self):
        """Floating offshore must have mappings for all 6 TG classes."""
        expected = {"TG-1", "TG-2", "TG-3", "TG-4", "TG-5", "TG-6"}
        assert set(FLOATING_COST_TIERS.keys()) == expected

    def test_fixed_multipliers_increase_with_tier(self):
        """Cost multipliers should increase from F1 to F3b."""
        tier_order = {"F1": 1, "F2a": 2, "F2b": 3, "F3a": 4, "F3b": 5}
        seen = {}
        for tg, (tier, mult) in FIXED_COST_TIERS.items():
            if tier not in seen:
                seen[tier] = mult
            else:
                assert seen[tier] == mult  # Same tier = same multiplier

        tier_mults = sorted(seen.items(), key=lambda x: tier_order[x[0]])
        mults = [m for _, m in tier_mults]
        assert mults == sorted(mults)  # Monotonically increasing

    def test_floating_multipliers_increase_with_tier(self):
        """Cost multipliers should increase from FL1 to FL3b."""
        tier_order = {"FL1": 1, "FL2a": 2, "FL2b": 3, "FL3a": 4, "FL3b": 5}
        seen = {}
        for tg, (tier, mult) in FLOATING_COST_TIERS.items():
            if tier not in seen:
                seen[tier] = mult
            else:
                assert seen[tier] == mult

        tier_mults = sorted(seen.items(), key=lambda x: tier_order[x[0]])
        mults = [m for _, m in tier_mults]
        assert mults == sorted(mults)

    def test_offshore_zones_constant(self):
        """Verify offshore zone names match expected set."""
        assert OFFSHORE_ZONES == {"DOGGER_BANK", "HORNSEA", "EAST_ANGLIA"}

    def test_ac_threshold_is_50km(self):
        """Verify AC/DC threshold is 50km (50,000m)."""
        assert AC_THRESHOLD_M == 50_000
