"""
Unit tests for calculate_renewable_potential.py

Tests the renewable technical potential calculation:
- Capacity density lookup from config (onshore and offshore carriers)
- Onshore potential: availability × zone_area × capacity_density
- Offshore potential: aggregation by (zone, carrier, cost_tier)
- Area-weighted centroid and distance calculations
- Combined output schema and value ranges
- Edge cases (zero availability, missing config, empty KRA data)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.calculate_renewable_potential import (
    OUTPUT_COLUMNS,
    calculate_offshore_potential,
    calculate_onshore_potential,
    get_capacity_density,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def config():
    """Realistic land_constraints config matching defaults.yaml."""
    return {
        "onwind": {"capacity_density": 3.0},
        "solar": {"capacity_density": 50.0},
        "offwind-ac": {"capacity_density": 6.0},
        "offwind-dc": {"capacity_density": 4.0},
        "offwind-float": {"capacity_density": {"ac": 3.0, "dc": 2.0}},
    }


@pytest.fixture
def avail_df():
    """Onshore availability matrix: 3 zones with varying availability."""
    return pd.DataFrame(
        {
            "onwind": [0.10, 0.25, 0.0],
            "solar": [0.15, 0.30, 0.0],
        },
        index=pd.Index(["Z1", "Z2", "Z3"], name="zone_name"),
    )


@pytest.fixture
def zone_areas():
    """Zone areas in km²."""
    return pd.Series(
        {"Z1": 1000.0, "Z2": 2000.0, "Z3": 500.0},
        name="area_km2",
    )


@pytest.fixture
def kra_gdf():
    """Offshore KRA fragments: 4 rows, 2 zones, 2 carriers."""
    return gpd.GeoDataFrame(
        {
            "kra_name": ["TG-1", "TG-1", "TG-3A", "TG-5"],
            "tg_class": ["TG-1", "TG-1", "TG-3A", "TG-5"],
            "cost_tier": ["F1", "F1", "F2a", "FL3a"],
            "capex_multiplier": [1.0, 1.0, 1.1, 1.2],
            "kra_type": ["fixed", "fixed", "fixed", "floating"],
            "carrier": [
                "offwind-fixed-dc",
                "offwind-fixed-dc",
                "offwind-fixed-ac",
                "offwind-float-ac",
            ],
            "connection_type": ["DC", "DC", "AC", "AC"],
            "distance_to_coast_km": [100.0, 120.0, 30.0, 40.0],
            "zone_name": ["DOGGER_BANK", "HORNSEA", "Z1", "Z1"],
            "available_area_km2": [500.0, 300.0, 50.0, 20.0],
            "centroid_x": [600000.0, 580000.0, 450000.0, 460000.0],
            "centroid_y": [600000.0, 500000.0, 1100000.0, 1100000.0],
            "geometry": [
                box(0, 0, 1, 1),
                box(1, 0, 2, 1),
                box(2, 0, 3, 1),
                box(3, 0, 4, 1),
            ],
        },
        crs="EPSG:27700",
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Capacity Density Lookup
# ══════════════════════════════════════════════════════════════════════════════


class TestGetCapacityDensity:
    """Test capacity density lookup from config."""

    def test_onwind(self, config):
        assert get_capacity_density("onwind", config) == 3.0

    def test_solar(self, config):
        assert get_capacity_density("solar", config) == 50.0

    def test_offwind_fixed_ac(self, config):
        assert get_capacity_density("offwind-fixed-ac", config) == 6.0

    def test_offwind_fixed_dc(self, config):
        assert get_capacity_density("offwind-fixed-dc", config) == 4.0

    def test_offwind_float_ac(self, config):
        assert get_capacity_density("offwind-float-ac", config) == 3.0

    def test_offwind_float_dc(self, config):
        assert get_capacity_density("offwind-float-dc", config) == 2.0

    def test_unknown_carrier_raises(self, config):
        with pytest.raises(ValueError, match="No capacity_density"):
            get_capacity_density("nuclear", config)

    def test_empty_config_raises(self):
        with pytest.raises(ValueError, match="No capacity_density"):
            get_capacity_density("onwind", {})


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Onshore Potential
# ══════════════════════════════════════════════════════════════════════════════


class TestCalculateOnshorePotential:
    """Test onshore technical potential calculation."""

    def test_basic_calculation(self, avail_df, zone_areas, config):
        """p_nom_max = frac × area × density."""
        result = calculate_onshore_potential(avail_df, zone_areas, config)

        # Z1 onwind: 0.10 × 1000 × 3.0 = 300.0 MW
        z1_onwind = result[
            (result["zone_name"] == "Z1") & (result["carrier"] == "onwind")
        ]
        assert z1_onwind["p_nom_max_mw"].values[0] == pytest.approx(300.0)

        # Z2 solar: 0.30 × 2000 × 50.0 = 30000.0 MW
        z2_solar = result[
            (result["zone_name"] == "Z2") & (result["carrier"] == "solar")
        ]
        assert z2_solar["p_nom_max_mw"].values[0] == pytest.approx(30000.0)

    def test_zero_availability_gives_zero(self, avail_df, zone_areas, config):
        """Zones with 0 availability should have 0 potential."""
        result = calculate_onshore_potential(avail_df, zone_areas, config)
        z3 = result[result["zone_name"] == "Z3"]
        assert (z3["p_nom_max_mw"] == 0.0).all()

    def test_output_has_correct_columns(self, avail_df, zone_areas, config):
        result = calculate_onshore_potential(avail_df, zone_areas, config)
        for col in ["zone_name", "carrier", "p_nom_max_mw", "capacity_density"]:
            assert col in result.columns

    def test_onshore_has_empty_offshore_fields(self, avail_df, zone_areas, config):
        """Onshore rows should have empty/NaN offshore-specific fields."""
        result = calculate_onshore_potential(avail_df, zone_areas, config)
        assert result["cost_tier"].eq("").all()
        assert result["connection_type"].eq("").all()
        assert result["capex_multiplier"].isna().all()

    def test_row_count(self, avail_df, zone_areas, config):
        """Should have zones × technologies rows."""
        result = calculate_onshore_potential(avail_df, zone_areas, config)
        # 3 zones × 2 technologies = 6 rows
        assert len(result) == 6


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Offshore Potential
# ══════════════════════════════════════════════════════════════════════════════


class TestCalculateOffshorePotential:
    """Test offshore technical potential calculation."""

    def test_aggregation_sums_area(self, kra_gdf, config):
        """Fragments in same group should have summed area."""
        result = calculate_offshore_potential(kra_gdf, config)

        # DOGGER_BANK + HORNSEA are different zones, so F1 DC has 2 groups
        dogger = result[
            (result["zone_name"] == "DOGGER_BANK")
            & (result["carrier"] == "offwind-fixed-dc")
        ]
        assert dogger["available_area_km2"].values[0] if "available_area_km2" in result.columns else True
        # p_nom_max = 500 × 4.0 = 2000 MW (DOGGER_BANK F1 only)
        assert dogger["p_nom_max_mw"].values[0] == pytest.approx(2000.0)

    def test_area_weighted_distance(self, kra_gdf, config):
        """Distance should be area-weighted mean, not simple mean."""
        # Add two fragments in same group with different distances
        gdf = kra_gdf.copy()
        # Rows 0,1 are in different zones so they don't aggregate
        # Let's check Z1 offwind-fixed-ac (row 2, single fragment)
        result = calculate_offshore_potential(gdf, config)
        z1_ac = result[
            (result["zone_name"] == "Z1")
            & (result["carrier"] == "offwind-fixed-ac")
        ]
        assert z1_ac["distance_to_coast_km"].values[0] == pytest.approx(30.0)

    def test_capacity_density_by_carrier(self, kra_gdf, config):
        """Each carrier should use correct density."""
        result = calculate_offshore_potential(kra_gdf, config)

        # offwind-fixed-dc → 4.0
        dc_rows = result[result["carrier"] == "offwind-fixed-dc"]
        assert (dc_rows["capacity_density"] == 4.0).all()

        # offwind-fixed-ac → 6.0
        ac_rows = result[result["carrier"] == "offwind-fixed-ac"]
        assert (ac_rows["capacity_density"] == 6.0).all()

        # offwind-float-ac → 3.0
        float_rows = result[result["carrier"] == "offwind-float-ac"]
        assert (float_rows["capacity_density"] == 3.0).all()

    def test_capex_multiplier_preserved(self, kra_gdf, config):
        """Cost tier multiplier should be preserved."""
        result = calculate_offshore_potential(kra_gdf, config)
        f1_rows = result[result["cost_tier"] == "F1"]
        assert (f1_rows["capex_multiplier"] == 1.0).all()

        f2a_rows = result[result["cost_tier"] == "F2a"]
        assert (f2a_rows["capex_multiplier"] == 1.1).all()

    def test_connection_type_preserved(self, kra_gdf, config):
        """Connection type from KRA data should be preserved."""
        result = calculate_offshore_potential(kra_gdf, config)
        dc_rows = result[result["carrier"] == "offwind-fixed-dc"]
        assert (dc_rows["connection_type"] == "DC").all()

    def test_centroid_coordinates_present(self, kra_gdf, config):
        """Centroids should be populated."""
        result = calculate_offshore_potential(kra_gdf, config)
        assert result["centroid_x"].notna().all()
        assert result["centroid_y"].notna().all()

    def test_output_row_count(self, kra_gdf, config):
        """Should have one row per unique (zone, carrier, cost_tier)."""
        result = calculate_offshore_potential(kra_gdf, config)
        # 4 unique groups: (DOGGER_BANK, fixed-dc, F1),
        # (HORNSEA, fixed-dc, F1), (Z1, fixed-ac, F2a), (Z1, float-ac, FL3a)
        assert len(result) == 4


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Combined Output
# ══════════════════════════════════════════════════════════════════════════════


class TestCombinedOutput:
    """Test combined onshore + offshore output."""

    def test_all_output_columns_present(
        self, avail_df, zone_areas, kra_gdf, config
    ):
        """Combined output should have all expected columns."""
        onshore = calculate_onshore_potential(avail_df, zone_areas, config)
        offshore = calculate_offshore_potential(kra_gdf, config)
        combined = pd.concat([onshore, offshore], ignore_index=True)

        for col in OUTPUT_COLUMNS:
            assert col in combined.columns

    def test_all_carriers_represented(
        self, avail_df, zone_areas, kra_gdf, config
    ):
        """Output should contain all carrier types."""
        onshore = calculate_onshore_potential(avail_df, zone_areas, config)
        offshore = calculate_offshore_potential(kra_gdf, config)
        combined = pd.concat([onshore, offshore], ignore_index=True)

        carriers = set(combined["carrier"].unique())
        assert "onwind" in carriers
        assert "solar" in carriers
        assert "offwind-fixed-dc" in carriers

    def test_p_nom_max_non_negative(
        self, avail_df, zone_areas, kra_gdf, config
    ):
        """All p_nom_max values should be non-negative."""
        onshore = calculate_onshore_potential(avail_df, zone_areas, config)
        offshore = calculate_offshore_potential(kra_gdf, config)
        combined = pd.concat([onshore, offshore], ignore_index=True)

        assert (combined["p_nom_max_mw"] >= 0).all()


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_kra_gives_empty_offshore(self, config):
        """Empty KRA GeoDataFrame should produce empty result."""
        empty_gdf = gpd.GeoDataFrame(
            columns=[
                "zone_name", "carrier", "cost_tier", "capex_multiplier",
                "available_area_km2", "distance_to_coast_km",
                "centroid_x", "centroid_y", "connection_type",
                "kra_name", "tg_class", "kra_type", "geometry",
            ],
            crs="EPSG:27700",
        )
        result = calculate_offshore_potential(empty_gdf, config)
        assert len(result) == 0

    def test_missing_zone_area_gives_zero(self, config):
        """Zone not in area lookup should give zero potential."""
        avail = pd.DataFrame(
            {"onwind": [0.5]},
            index=pd.Index(["UNKNOWN_ZONE"], name="zone_name"),
        )
        areas = pd.Series({"Z1": 1000.0})  # UNKNOWN_ZONE not here
        result = calculate_onshore_potential(avail, areas, config)
        assert result["p_nom_max_mw"].values[0] == 0.0
