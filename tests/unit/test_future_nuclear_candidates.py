"""
Unit tests for future nuclear candidate helpers and thermal-path integration.
"""

import logging
import sys
from pathlib import Path

import pandas as pd
import pypsa
import pytest


PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from generators.future_capacity_candidates import load_fes_nuclear_capacity_split  # noqa: E402
from generators.future_nuclear_candidates import (  # noqa: E402
    build_large_nuclear_candidate_rows,
    build_large_nuclear_headroom_table,
    build_smr_candidate_headroom_table,
    build_smr_candidate_rows,
)
from generators.integrate_thermal_generators import add_thermal_generators  # noqa: E402
from generators.integrate_thermal_generators import build_future_nuclear_report_table  # noqa: E402


@pytest.mark.parametrize(
    "workbook_name",
    [
        "Future Energy Scenarios 2024 Data Workbook_V006_0.xlsx",
        "Future Energy Scenarios 2025 Data Workbook V005_0.xlsx",
    ],
)
def test_local_fes_workbooks_expose_large_and_small_nuclear(workbook_name):
    """Checked-in FES workbooks should return separate large and small nuclear totals."""
    workbook_path = PROJECT_ROOT / "data" / "FES" / workbook_name
    assert workbook_path.exists()

    result = load_fes_nuclear_capacity_split(
        workbook_path=workbook_path,
        modelled_year=2035,
        fes_scenario="Holistic Transition",
    )

    assert "large_nuclear_mw" in result
    assert "smr_mw" in result
    assert result["large_nuclear_mw"] > 0
    assert result["smr_mw"] >= 0


def _write_minimal_es1_workbook(tmp_path: Path, values: list[object]) -> Path:
    workbook_path = tmp_path / "fes_es1_test.xlsx"
    rows = [
        ["metadata", None, None, None, None, None, None],
        ["Connection", "Pathway", "Variable", "Category", "Type", "SubType", 2035],
        ["Transmission", "Holistic Transition", "Capacity (MW)", "Electricity", "Nuclear", "Nuclear - Large", values[0]],
        ["Transmission", "Holistic Transition", "Capacity (MW)", "Electricity", "Nuclear", "Nuclear - Small", values[1]],
    ]
    pd.DataFrame(rows).to_excel(workbook_path, sheet_name="ES1", header=False, index=False)
    return workbook_path


def test_fes_nuclear_reader_rejects_negative_capacity(tmp_path):
    """Selected FES workbook capacity cells should fail early if negative."""
    workbook_path = _write_minimal_es1_workbook(tmp_path, [1000.0, -1.0])

    with pytest.raises(ValueError, match="negative values"):
        load_fes_nuclear_capacity_split(
            workbook_path=workbook_path,
            modelled_year=2035,
            fes_scenario="Holistic Transition",
        )


def test_fes_nuclear_reader_rejects_malformed_capacity(tmp_path):
    """Selected FES workbook capacity cells should not be silently coerced to zero."""
    workbook_path = _write_minimal_es1_workbook(tmp_path, ["not-a-number", 10.0])

    with pytest.raises(ValueError, match="non-numeric or missing values"):
        load_fes_nuclear_capacity_split(
            workbook_path=workbook_path,
            modelled_year=2035,
            fes_scenario="Holistic Transition",
        )


def test_large_nuclear_headroom_respects_site_cap_and_grandfathered_baseline():
    """Live existing nuclear should occupy local headroom without being reduced."""
    en6_sites = pd.DataFrame(
        [
            {
                "site_id": "hartlepool",
                "site_name": "Hartlepool",
                "site_status": "current",
                "lat": 54.63,
                "lon": -1.18,
                "country": "England",
                "existing_name_patterns": "Hartlepool",
            },
            {
                "site_id": "torness",
                "site_name": "Torness",
                "site_status": "current",
                "lat": 55.96,
                "lon": -2.40,
                "country": "Scotland",
                "existing_name_patterns": "Torness",
            },
        ]
    )
    existing = pd.DataFrame(
        [
            {"site_name": "Hartlepool", "capacity_mw": 1600.0},
            {"site_name": "Torness", "capacity_mw": 1700.0},
        ]
    )

    headroom = build_large_nuclear_headroom_table(
        en6_sites_df=en6_sites,
        existing_sites_df=existing,
        site_cap_mw=3400.0,
        scotland_ban=True,
    )

    assert headroom["site_id"].tolist() == ["hartlepool"]
    row = headroom.iloc[0]
    assert row["live_existing_capacity_mw"] == pytest.approx(1600.0)
    assert row["local_siting_cap_mw"] == pytest.approx(3400.0)
    assert row["local_headroom_mw"] == pytest.approx(1800.0)
    assert bool(row["oversubscribed"]) is False


def test_smr_allocation_uses_anchor_weights_and_reports_unallocated_zones():
    """SMR zonal land caps should split across anchors and flag stranded zones."""
    anchors = pd.DataFrame(
        [
            {
                "anchor_id": "a1",
                "anchor_name": "Anchor 1",
                "anchor_type": "industrial",
                "bus": "Z1",
                "zone_name": "Z1",
                "lat": 54.0,
                "lon": -1.0,
                "electricity_demand_mw": 100.0,
                "gas_demand_mw": 50.0,
            },
            {
                "anchor_id": "a2",
                "anchor_name": "Anchor 2",
                "anchor_type": "port",
                "bus": "Z1",
                "zone_name": "Z1",
                "lat": 54.1,
                "lon": -1.1,
                "electricity_demand_mw": 50.0,
                "gas_demand_mw": 25.0,
            },
        ]
    )
    zone_caps = pd.DataFrame(
        [
            {"zone_name": "Z1", "zone_land_cap_mw": 300.0},
            {"zone_name": "Z2", "zone_land_cap_mw": 120.0},
        ]
    )

    eligible, unallocated = build_smr_candidate_headroom_table(
        anchors_df=anchors,
        zone_land_caps_df=zone_caps,
        national_fes_total_mw=180.0,
        demand_weights={"electricity": 1.0, "gas": 1.0},
        zone_col="zone_name",
    )

    assert len(eligible) == 2
    assert eligible["anchor_fes_share_mw"].sum() == pytest.approx(180.0)
    assert eligible["anchor_land_cap_mw"].sum() == pytest.approx(300.0)
    assert unallocated["zone_name"].tolist() == ["Z2"]


def test_candidate_row_builders_preserve_bus_and_metadata():
    """Future nuclear candidate rows should keep the chosen bus and metadata fields."""
    large_rows = build_large_nuclear_candidate_rows(
        pd.DataFrame(
            [
                {
                    "site_id": "sizewell",
                    "site_name": "Sizewell",
                    "bus": "Z4",
                    "lat": 52.2,
                    "lon": 1.6,
                    "local_headroom_mw": 1800.0,
                    "local_siting_cap_mw": 3400.0,
                }
            ]
        ),
        modelled_year=2035,
        capital_cost=8000.0,
    )
    smr_rows = build_smr_candidate_rows(
        pd.DataFrame(
            [
                {
                    "anchor_id": "teesside",
                    "anchor_name": "Teesside",
                    "anchor_type": "industrial",
                    "bus": "Z1",
                    "lat": 54.57,
                    "lon": -1.2,
                    "anchor_fes_share_mw": 250.0,
                    "anchor_land_cap_mw": 150.0,
                }
            ]
        ),
        modelled_year=2035,
        capital_cost=9500.0,
    )

    assert large_rows.iloc[0]["bus"] == "Z4"
    assert large_rows.iloc[0]["future_candidate_group"] == "large_nuclear"
    assert smr_rows.iloc[0]["bus"] == "Z1"
    assert smr_rows.iloc[0]["future_candidate_group"] == "smr"
    assert smr_rows.iloc[0]["future_candidate_land_cap_mw"] == pytest.approx(150.0)


def test_future_nuclear_report_table_keeps_unique_columns():
    """SMR report standardization should not create duplicate column names."""
    large_headroom = pd.DataFrame(
        [
            {
                "site_id": "sizewell",
                "site_name": "Sizewell",
                "bus": "Z4",
                "local_siting_cap_mw": 3400.0,
                "live_existing_capacity_mw": 1200.0,
                "local_headroom_mw": 2200.0,
                "effective_total_cap_mw": 3400.0,
                "extendable_headroom_pre_land_mw": 2200.0,
                "extendable_headroom_mw": 2200.0,
                "oversubscribed": False,
                "oversubscription_amount_mw": 0.0,
            }
        ]
    )
    smr_headroom = pd.DataFrame(
        [
            {
                "anchor_id": "teesside",
                "anchor_name": "Teesside",
                "anchor_type": "industrial",
                "bus": "Z1",
                "zone_name": "Z1",
                "fes_group_total_mw": 500.0,
                "zone_land_cap_mw": 300.0,
                "anchor_fes_share_mw": 250.0,
                "anchor_land_cap_mw": 150.0,
                "live_existing_capacity_mw": 0.0,
                "local_headroom_mw": 150.0,
                "effective_total_cap_mw": 300.0,
                "extendable_headroom_pre_land_mw": 250.0,
                "extendable_headroom_mw": 150.0,
                "oversubscribed": False,
                "oversubscription_amount_mw": 0.0,
            }
        ]
    )
    smr_unallocated = pd.DataFrame([{"zone_name": "Z2", "unallocated_zone": True}])

    report = build_future_nuclear_report_table(
        large_headroom=large_headroom,
        smr_headroom=smr_headroom,
        smr_unallocated=smr_unallocated,
        smr_group_total_mw=500.0,
    )

    assert report.columns.is_unique
    assert "row_level_land_cap_mw" in report.columns
    smr_rows = report[report["carrier"] == "smr"]
    assert float(smr_rows.iloc[0]["local_headroom_mw"]) == pytest.approx(150.0)
    assert float(smr_rows.iloc[0]["row_level_land_cap_mw"]) == pytest.approx(150.0)


def test_add_thermal_generators_keeps_zero_capacity_extendable_candidate():
    """Thermal integration should keep zero-capacity extendable future nuclear candidates."""
    network = pypsa.Network()
    network.add("Bus", "Z1", x=-1.0, y=54.0)

    thermal_data = pd.DataFrame(
        [
            {
                "station_name": "FESCandidate_smr_teesside_2035",
                "fuel_type": "smr",
                "capacity_mw": 0.0,
                "bus": "Z1",
                "p_nom_extendable": True,
                "p_nom_min": 0.0,
                "p_nom_max": 150.0,
                "capital_cost": 9500.0,
                "future_candidate_group": "smr",
                "future_candidate_anchor_id": "teesside",
                "future_candidate_land_cap_mw": 120.0,
            }
        ]
    )

    updated = add_thermal_generators(
        network,
        thermal_data,
        fuel_data_path=None,
        solve_mode="LP",
    )

    assert "FESCandidate_smr_teesside_2035" in updated.generators.index
    row = updated.generators.loc["FESCandidate_smr_teesside_2035"]
    assert row["carrier"] == "smr"
    assert bool(row["p_nom_extendable"]) is True
    assert row["p_nom"] == pytest.approx(0.0)
    assert row["p_nom_max"] == pytest.approx(150.0)
    assert row["future_candidate_group"] == "smr"
    assert row["future_candidate_anchor_id"] == "teesside"
