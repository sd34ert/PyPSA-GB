# Location-Constraint Logic for Developers

This page documents the current siting and candidate-cap logic for contributors working on future capacity candidates, policy tightening, or post-solve reporting.

## Where the Logic Lives

### Candidate-envelope construction

- `scripts/generators/future_capacity_candidates.py`
  - generic FES and land-cap headroom logic for renewables
- `scripts/generators/integrate_renewable_generators.py`
  - builds renewable future candidate rows and the pre-policy oversubscription report

### Future nuclear candidate construction

- `scripts/generators/future_nuclear_candidates.py`
  - large-nuclear site headroom
  - SMR anchor allocation
- `scripts/generators/integrate_thermal_generators.py`
  - assembles large nuclear and SMR candidate rows
  - writes the thermal-stage future-capacity report

### Policy tightening

- `scripts/generators/apply_technical_potential_constraints.py`
  - is the authoritative stage for final candidate `p_nom_max`
  - recomputes final new-build headroom on the fully assembled network
  - produces `resources/network/{scenario}_network_constrained.nc`

### Post-policy reporting

- `scripts/analysis/generate_location_constraint_report.py`
  - compares the pre-policy candidate report, the post-policy network, and the solved network
  - assigns `constraint_outcome`

## Current Envelope Logic

The upstream generator stages keep the raw FES allocation, live existing
capacity, and land-cap metadata so that candidate geography and provenance are
auditable. These upstream rows are pre-policy diagnostics; they are not the
final solve authority. The downstream policy stage applies the authoritative
network cap before solve.

For candidate rows with a land cap, the downstream policy interpretation is:

```text
effective_total_cap_mw = min(fes_spatial_cap_mw, land_cap_mw)
extendable_headroom_mw = max(effective_total_cap_mw - live_existing_capacity_mw, 0)
```

This means:

- FES acts as an upper-bound build envelope
- land can tighten that envelope
- existing fixed capacity is grandfathered and consumes headroom
- oversubscribed rows lose candidate headroom but keep existing assets

Oversubscription is therefore not always pure evidence of physical land
scarcity. If the FES spatial split allocated capacity to a zone before existing
capacity was deducted, a row can be flagged as a possible pre-allocation
artefact. The post-policy report exposes this through `land_binding`,
`existing_oversubscribed`, and `possible_preallocation_artifact`.

The current solve does not enforce a national equality target such as
`sum(p_nom) == target_mw`. Future nuclear shared caps are upper-bound
inequalities, so the optimiser may build less than the FES envelope where that
is the least-cost outcome. A forced national redistribution/equality design
would be a separate diagnostic sensitivity, not the default model.

## Carrier-specific Semantics

### Renewables

- `land_cap_mw` comes from the zonal technical-potential CSV after carrier mapping
- `row_level_land_cap_mw` is intentionally blank
- report rows are one candidate per `(carrier, bus)`

### Large nuclear

- `land_cap_mw` is the configured per-site cap from:
  - `nuclear.siting_constraints.large_nuclear.max_per_site_mw`
- the current default is `3400 MW`
- this is not sourced from the GIS zonal technical-potential CSV
- current implementation treats `3400 MW` as a repo assumption, not a cited project capacity

### SMR

- `land_cap_mw` is the total zonal SMR cap
- `row_level_land_cap_mw` is the anchor-level share of that zonal cap
- anchor shares are weighted by configured electricity and gas demand weights
- `unallocated_zone = True` means:
  - zonal SMR land potential exists
  - but no eligible SMR anchor exists in that zone
  - so no candidate row is created for that zone

## Report-State Semantics

The post-policy report currently classifies rows as:

- `unchanged`
- `no_candidate_build`
- `oversubscribed_blocked`
- `tightened_nonbinding`
- `tightened_and_built`

Current meanings in code:

- `oversubscribed_blocked`: oversubscribed row and post-policy cap effectively zero
- `tightened_and_built`: policy tightened the cap and solved build remains positive
- `tightened_nonbinding`: policy tightened the cap but solved build is zero
- `no_candidate_build`: no policy tightening and no solved build
- `unchanged`: no policy tightening and positive solved build

## Important Implementation Constraints

- Pre-policy and post-policy reports are intentionally different stages; do not collapse them into one file.
- `resources/generators/{scenario}_future_capacity_oversubscription.csv` can be identical between constrained and unconstrained scenarios.
- `resources/analysis/{scenario}_location_constraint_report.csv` is the authoritative post-policy, solved-aware row-level explanation.
- Future extendable candidates intentionally start at `p_nom = 0`; validation must check extendability and positive `p_nom_max`, not fixed-asset rules.
- SMR report tables must preserve unique column names; `local_headroom_mw` and `row_level_land_cap_mw` are different concepts.

## Key Data Inputs

- `resources/land/technical_potential_Zonal.csv`
- `data/generators/nuclear_en6_sites.csv`
- `data/generators/smr_demand_anchors.csv`
- `data/hydrogen/topology/*.csv`

Keep the semantics of these inputs explicit in code and docs. The siting workflow is now sensitive to whether a cap is:

- zonal
- site-level
- anchor-level
- pre-policy
- post-policy
