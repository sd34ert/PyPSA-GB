"""
Unit tests for downstream technical-potential policy tightening.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
import pytest


PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from generators.apply_technical_potential_constraints import (  # noqa: E402
    apply_technical_potential_constraints,
)


def _network_with_candidate_and_fixed(existing_mw: float, candidate_limit_mw: float) -> pypsa.Network:
    network = pypsa.Network()
    network.add("Bus", "Z2", v_nom=400, x=-3.0, y=57.0)
    network.add(
        "Generator",
        "Existing wind_onshore Z2",
        bus="Z2",
        carrier="wind_onshore",
        p_nom=existing_mw,
        p_nom_extendable=False,
    )
    network.add(
        "Generator",
        "FESCandidate_wind_onshore_Z2_2035",
        bus="Z2",
        carrier="wind_onshore",
        p_nom=0.0,
        p_nom_extendable=True,
        p_nom_max=candidate_limit_mw,
    )
    network.generators["future_candidate_land_cap_mw"] = np.nan
    return network


def test_downstream_policy_stage_sets_final_candidate_p_nom_max():
    """The policy layer should be the authoritative source of final candidate caps."""
    network = _network_with_candidate_and_fixed(existing_mw=60.0, candidate_limit_mw=140.0)
    technical_potential = pd.DataFrame(
        [{"zone_name": "Z2", "carrier": "onwind", "p_nom_max_mw": 80.0}]
    )

    constrained, stats = apply_technical_potential_constraints(network, technical_potential)

    row = constrained.generators.loc["FESCandidate_wind_onshore_Z2_2035"]
    assert row["p_nom_max"] == pytest.approx(20.0)
    assert row["pre_policy_p_nom_max_mw"] == pytest.approx(140.0)
    assert row["post_policy_p_nom_max_mw"] == pytest.approx(20.0)
    assert row["policy_land_cap_mw"] == pytest.approx(80.0)
    assert row["policy_live_existing_capacity_mw"] == pytest.approx(60.0)
    assert bool(row["policy_existing_oversubscribed"]) is False
    assert bool(row["policy_possible_preallocation_artifact"]) is False
    assert stats["candidate_constraints_tightened"] == 1


def test_oversubscribed_existing_capacity_blocks_new_build_not_fixed_assets():
    """Existing assets should be grandfathered while new-build headroom is set to zero."""
    network = _network_with_candidate_and_fixed(existing_mw=120.0, candidate_limit_mw=50.0)
    technical_potential = pd.DataFrame(
        [{"zone_name": "Z2", "carrier": "onwind", "p_nom_max_mw": 80.0}]
    )

    constrained, stats = apply_technical_potential_constraints(network, technical_potential)

    fixed = constrained.generators.loc["Existing wind_onshore Z2"]
    candidate = constrained.generators.loc["FESCandidate_wind_onshore_Z2_2035"]
    assert fixed["p_nom"] == pytest.approx(120.0)
    assert candidate["p_nom_max"] == pytest.approx(0.0)
    assert bool(candidate["policy_existing_oversubscribed"]) is True
    assert bool(candidate["policy_possible_preallocation_artifact"]) is True
    assert stats["candidate_oversubscribed_rows"] == 1
    assert stats["possible_preallocation_artifact_rows"] == 1
