"""
Unit tests for the post-policy location-constraint report.
"""

import sys
from pathlib import Path

import pandas as pd
import pypsa
import pytest


PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from analysis.generate_location_constraint_report import (  # noqa: E402
    build_location_constraint_report,
    empty_location_constraint_report,
)


def _network_with_candidate(p_nom_max: float, p_nom_opt: float | None = None) -> pypsa.Network:
    network = pypsa.Network()
    network.set_snapshots(pd.date_range("2035-01-01", periods=1, freq="h"))
    network.add("Bus", "Z2", v_nom=400, x=-3.0, y=57.0)
    network.add(
        "Generator",
        "FESCandidate_wind_offshore_Z2_2035",
        bus="Z2",
        carrier="wind_offshore",
        p_nom=0.0,
        p_nom_extendable=True,
        p_nom_max=p_nom_max,
        marginal_cost=0.0,
    )
    if p_nom_opt is not None:
        network.generators["p_nom_opt"] = 0.0
        network.generators.loc["FESCandidate_wind_offshore_Z2_2035", "p_nom_opt"] = p_nom_opt
    return network


def test_location_constraint_report_marks_oversubscribed_blocked():
    """Oversubscribed rows with zero post-policy headroom should be obvious in one row."""
    pre_policy_network = _network_with_candidate(25660.721736323558)
    post_policy_network = _network_with_candidate(0.0)
    solved_network = _network_with_candidate(0.0, p_nom_opt=0.0)
    pre_policy_report_df = pd.DataFrame(
        [
            {
                "carrier": "wind_offshore",
                "candidate_group": "",
                "zone_name": "Z2",
                "bus": "Z2",
                "site_or_anchor_id": "",
                "site_or_anchor_name": "",
                "anchor_type": "",
                "fes_spatial_cap_mw": 27862.021736,
                "land_cap_mw": 6.36,
                "row_level_land_cap_mw": None,
                "effective_total_cap_mw": 6.36,
                "live_existing_capacity_mw": 2201.3,
                "extendable_headroom_pre_land_mw": 25660.721736,
                "extendable_headroom_mw": 0.0,
                "oversubscribed": True,
                "oversubscription_amount_mw": 2194.94,
            }
        ]
    )

    report = build_location_constraint_report(
        pre_policy_network=pre_policy_network,
        post_policy_network=post_policy_network,
        solved_network=solved_network,
        pre_policy_report_df=pre_policy_report_df,
    )

    assert len(report) == 1
    row = report.iloc[0]
    assert row["candidate_name"] == "FESCandidate_wind_offshore_Z2_2035"
    assert row["p_nom_max_pre_policy_mw"] == pytest.approx(25660.721736323558)
    assert row["p_nom_max_post_policy_mw"] == pytest.approx(0.0)
    assert row["pre_policy_p_nom_max_mw"] == pytest.approx(25660.721736323558)
    assert row["post_policy_p_nom_max_mw"] == pytest.approx(0.0)
    assert row["policy_headroom_removed_mw"] == pytest.approx(25660.721736323558)
    assert bool(row["policy_constraint_binding"]) is True
    assert bool(row["land_binding"]) is True
    assert bool(row["existing_oversubscribed"]) is True
    assert bool(row["possible_preallocation_artifact"]) is True
    assert row["constraint_outcome"] == "oversubscribed_blocked"


def test_location_constraint_report_marks_no_candidate_build_when_policy_does_not_tighten():
    """Unchanged headroom with zero solved build should be labelled clearly."""
    pre_policy_network = _network_with_candidate(120.0)
    post_policy_network = _network_with_candidate(120.0)
    solved_network = _network_with_candidate(120.0, p_nom_opt=0.0)
    pre_policy_report_df = pd.DataFrame(
        [
            {
                "carrier": "wind_offshore",
                "candidate_group": "",
                "zone_name": "Z2",
                "bus": "Z2",
                "site_or_anchor_id": "",
                "site_or_anchor_name": "",
                "anchor_type": "",
                "fes_spatial_cap_mw": 150.0,
                "land_cap_mw": 150.0,
                "row_level_land_cap_mw": None,
                "effective_total_cap_mw": 150.0,
                "live_existing_capacity_mw": 30.0,
                "extendable_headroom_pre_land_mw": 120.0,
                "extendable_headroom_mw": 120.0,
                "oversubscribed": False,
                "oversubscription_amount_mw": 0.0,
            }
        ]
    )

    report = build_location_constraint_report(
        pre_policy_network=pre_policy_network,
        post_policy_network=post_policy_network,
        solved_network=solved_network,
        pre_policy_report_df=pre_policy_report_df,
    )

    row = report.iloc[0]
    assert bool(row["policy_constraint_binding"]) is False
    assert bool(row["land_binding"]) is False
    assert bool(row["existing_oversubscribed"]) is False
    assert bool(row["possible_preallocation_artifact"]) is False
    assert row["constraint_outcome"] == "no_candidate_build"


def test_location_constraint_report_handles_empty_candidate_sets():
    """Historical or non-candidate scenarios should still produce a schema-valid report."""
    report = build_location_constraint_report(
        pre_policy_network=pypsa.Network(),
        post_policy_network=pypsa.Network(),
        solved_network=pypsa.Network(),
        pre_policy_report_df=empty_location_constraint_report(),
    )

    assert list(report.columns) == list(empty_location_constraint_report().columns)
    assert report.empty
