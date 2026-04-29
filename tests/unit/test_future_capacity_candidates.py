"""
Unit tests for future FES capacity candidate helpers.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from generators.future_capacity_candidates import (  # noqa: E402
    build_candidate_headroom_table,
    build_candidate_rows,
    build_land_cap_table,
)


def test_build_land_cap_table_sums_offshore_rows():
    technical_potential = pd.DataFrame(
        [
            {"zone_name": "Z1", "carrier": "offwind-fixed-ac", "p_nom_max_mw": 100.0},
            {"zone_name": "Z1", "carrier": "offwind-fixed-dc", "p_nom_max_mw": 50.0},
            {"zone_name": "Z1", "carrier": "solar", "p_nom_max_mw": 75.0},
        ]
    )

    result = build_land_cap_table(technical_potential)

    offshore = result[(result["carrier"] == "wind_offshore") & (result["zone_name"] == "Z1")]
    solar = result[(result["carrier"] == "solar_pv") & (result["zone_name"] == "Z1")]

    assert offshore["land_cap_mw"].iloc[0] == pytest.approx(150.0)
    assert solar["land_cap_mw"].iloc[0] == pytest.approx(75.0)


def test_build_land_cap_table_keeps_smr_separate_from_large_nuclear():
    technical_potential = pd.DataFrame(
        [
            {"zone_name": "Z1", "carrier": "smr", "p_nom_max_mw": 120.0},
            {"zone_name": "Z1", "carrier": "nuclear-large", "p_nom_max_mw": 3400.0},
        ]
    )

    result = build_land_cap_table(technical_potential)

    smr = result[(result["carrier"] == "smr") & (result["zone_name"] == "Z1")]
    large = result[(result["carrier"] == "nuclear") & (result["zone_name"] == "Z1")]

    assert smr["land_cap_mw"].iloc[0] == pytest.approx(120.0)
    assert large["land_cap_mw"].iloc[0] == pytest.approx(3400.0)


def test_headroom_uses_tighter_of_fes_and_land():
    fes_caps = pd.DataFrame(
        [
            {"carrier": "wind_onshore", "bus": "Z1", "lat": 0.0, "lon": 0.0, "capacity_mw": 100.0},
        ]
    )
    baseline = pd.DataFrame(
        [
            {"carrier": "wind_onshore", "bus": "Z1", "lat": 0.0, "lon": 0.0, "capacity_mw": 30.0},
        ]
    )
    land_caps = pd.DataFrame(
        [
            {"carrier": "wind_onshore", "zone_name": "Z1", "land_cap_mw": 80.0},
        ]
    )

    result = build_candidate_headroom_table(
        fes_capacity_df=fes_caps,
        baseline_capacity_df=baseline,
        candidate_carriers=["wind_onshore"],
        land_caps_df=land_caps,
    )

    row = result.iloc[0]
    assert row["fes_spatial_cap_mw"] == pytest.approx(100.0)
    assert row["land_cap_mw"] == pytest.approx(80.0)
    assert row["effective_total_cap_mw"] == pytest.approx(80.0)
    assert row["extendable_headroom_pre_land_mw"] == pytest.approx(70.0)
    assert row["extendable_headroom_mw"] == pytest.approx(50.0)
    assert bool(row["land_binding"]) is True
    assert bool(row["existing_oversubscribed"]) is False
    assert bool(row["possible_preallocation_artifact"]) is False


def test_headroom_reports_oversubscription_when_existing_exceeds_cap():
    fes_caps = pd.DataFrame(
        [
            {"carrier": "solar_pv", "bus": "Z2", "lat": 0.0, "lon": 0.0, "capacity_mw": 40.0},
        ]
    )
    baseline = pd.DataFrame(
        [
            {"carrier": "solar_pv", "bus": "Z2", "lat": 0.0, "lon": 0.0, "capacity_mw": 55.0},
        ]
    )
    land_caps = pd.DataFrame(
        [
            {"carrier": "solar_pv", "zone_name": "Z2", "land_cap_mw": 45.0},
        ]
    )

    result = build_candidate_headroom_table(
        fes_capacity_df=fes_caps,
        baseline_capacity_df=baseline,
        candidate_carriers=["solar_pv"],
        land_caps_df=land_caps,
    )

    row = result.iloc[0]
    assert row["effective_total_cap_mw"] == pytest.approx(40.0)
    assert bool(row["oversubscribed"]) is True
    assert bool(row["existing_oversubscribed"]) is True
    assert row["oversubscription_amount_mw"] == pytest.approx(15.0)
    assert row["extendable_headroom_mw"] == pytest.approx(0.0)
    assert bool(row["possible_preallocation_artifact"]) is False


def test_build_candidate_rows_uses_pre_land_headroom():
    headroom_df = pd.DataFrame(
        [
            {
                "carrier": "wind_onshore",
                "bus": "Z3",
                "lat": 52.0,
                "lon": -1.0,
                "extendable_headroom_pre_land_mw": 25.0,
            },
            {
                "carrier": "solar_pv",
                "bus": "Z4",
                "lat": 53.0,
                "lon": -2.0,
                "extendable_headroom_pre_land_mw": 0.0,
            },
        ]
    )

    candidates = build_candidate_rows(
        headroom_df=headroom_df,
        modelled_year=2035,
        capital_costs={"wind_onshore": 123.0},
    )

    assert len(candidates) == 1
    row = candidates.iloc[0]
    assert row["site_name"] == "FESCandidate_wind_onshore_Z3_2035"
    assert bool(row["p_nom_extendable"]) is True
    assert row["p_nom_max"] == pytest.approx(25.0)
    assert row["capital_cost"] == pytest.approx(123.0)
    assert "future_candidate_fes_spatial_cap_mw" in row.index
    assert "future_candidate_zonal_land_cap_mw" in row.index
