# Location Constraints and Candidate Reports

This page explains how PyPSA-GB currently interprets future siting limits for candidate generators and how to read the two main siting reports:

- `resources/generators/{scenario}_future_capacity_oversubscription.csv`
- `resources/analysis/{scenario}_location_constraint_report.csv`

The first is a **pre-policy candidate-envelope report**. The second is a **post-policy, solved-aware report** that shows what actually changed before solve and whether the change mattered in optimization.

## Two Report Stages

### Pre-policy report

`resources/generators/{scenario}_future_capacity_oversubscription.csv`

This file is written during generator integration. It combines:

- future FES build envelopes
- live existing capacity already occupying a zone or site
- land-cap metadata used later by the policy layer

It is a diagnostic provenance file, not the final source of solve caps.
This file can be identical between constrained and unconstrained scenarios, because it is written **before** `technical_potential_constraints.enabled` mutates the assembled network.

### Post-policy report

`resources/analysis/{scenario}_location_constraint_report.csv`

This file joins:

- the pre-policy report
- the assembled pre-policy network
- the finalized post-policy network
- the solved network

This is the best report for understanding why `HT35_zonal` and `HT35_zonal_constrained` differ.

## Core Logic

For candidate rows with a land cap, the current repo uses:

```text
effective_total_cap_mw = min(fes_spatial_cap_mw, land_cap_mw)
extendable_headroom_mw = max(effective_total_cap_mw - live_existing_capacity_mw, 0)
```

If no land cap exists, the effective cap falls back to the FES spatial cap.

Important behavior:

- Existing fixed capacity is **grandfathered**.
- Oversubscribed rows do **not** retire existing capacity.
- Oversubscribed rows simply lose all **new-build** headroom.
- The final `p_nom_max` used by the solve is set downstream by the policy stage.
- FES future capacity is treated as an upper-bound envelope, not as a forced national equality target.

## Column Meanings

### Envelope columns

- `fes_spatial_cap_mw`: the future FES capacity already spatially allocated to the row's bus, zone, site, or anchor
- `land_cap_mw`: the land-based cap used by the row
- `row_level_land_cap_mw`: a row-specific share of the land cap
- `effective_total_cap_mw`: the tighter of FES and land where land exists
- `live_existing_capacity_mw`: existing fixed capacity already occupying that row's envelope
- `extendable_headroom_pre_land_mw`: headroom before land is applied
- `extendable_headroom_mw`: headroom after land is applied
- `land_binding`: whether the land cap is tighter than the FES spatial allocation
- `existing_oversubscribed`: whether existing fixed capacity already exceeds the effective cap
- `possible_preallocation_artifact`: whether a zero-headroom row may partly reflect the FES spatial split being allocated before existing capacity was deducted

### Optimization columns

- `pre_policy_p_nom_max_mw`: candidate cap before downstream policy tightening
- `post_policy_p_nom_max_mw`: candidate cap after downstream policy tightening
- `p_nom_max_pre_policy_mw`: candidate cap before the policy step
- `p_nom_max_post_policy_mw`: candidate cap after the policy step
- `p_nom_opt_mw`: optimized build from the solved network

The duplicated pre/post naming is intentional for compatibility with older
reports while making the policy-stage meaning explicit in new outputs.

## Decision Cases

### Case 1: land above FES, existing below cap

- `effective_total_cap_mw = fes_spatial_cap_mw`
- land does not tighten the candidate
- the solve may build up to the FES-derived headroom

This is the dominant current pattern for `wind_onshore` in `HT35_zonal_constrained`.

### Case 2: land below FES, existing below land

- `effective_total_cap_mw = land_cap_mw`
- the candidate survives, but with a tighter cap
- this is where constrained and unconstrained runs can diverge materially

### Case 3: existing above FES

- even without land, the row is already full relative to the FES envelope
- `extendable_headroom_mw = 0`
- no further build is allowed there

### Case 4: existing above effective cap

- this is the strongest restriction
- the row is oversubscribed
- the policy step can tighten `p_nom_max_post_policy_mw` to zero
- existing capacity remains in place, but the optimizer cannot add more there

## Outcome Labels in the Post-policy Report

The current code uses these labels:

- `unchanged`: the policy layer did not tighten the candidate and the solve built it
- `no_candidate_build`: the candidate remained available but the solve did not build it
- `oversubscribed_blocked`: the row was oversubscribed and the post-policy candidate headroom was tightened to zero
- `tightened_nonbinding`: valid report state when the policy tightens a row but the solve still does not build it
- `tightened_and_built`: valid report state when the policy tightens a row and the solve still builds some capacity there

The current HT35 constrained report mainly uses:

- `unchanged`
- `no_candidate_build`
- `oversubscribed_blocked`

## Carrier-specific Notes

### Renewables

- `land_cap_mw` is zonal
- `row_level_land_cap_mw` is currently blank

### Large nuclear

- `land_cap_mw` is currently the configured per-site cap
- this does **not** come from GIS zonal technical potential
- the current default is `3400 MW` per large-nuclear site
- this is a repo assumption, not a source-backed project capacity

### SMR

- `land_cap_mw` is the total zonal SMR cap
- `row_level_land_cap_mw` is the anchor-level share of that zonal cap
- `unallocated_zone = True` means the zone had SMR land potential but no eligible anchor in the current SMR anchor table

## Worked Example: `wind_offshore @ Z2`

From the current HT35 artifacts:

- `fes_spatial_cap_mw = 27862.021736`
- `land_cap_mw = 6.36`
- `effective_total_cap_mw = 6.36`
- `live_existing_capacity_mw = 2201.3`
- `extendable_headroom_pre_land_mw = 25660.721736`
- `extendable_headroom_mw = 0.0`

Interpretation:

- FES allocates a large future offshore envelope to `Z2`
- the land-derived cap for that row is tiny
- existing offshore capacity already exceeds that cap
- the row is therefore oversubscribed
- the policy layer removes all new-build headroom there

In the post-policy report:

- baseline `HT35_zonal`: candidate remains available, but the solve still does not use it
- constrained `HT35_zonal_constrained`: `p_nom_max_post_policy_mw = 0.0` and the row is labelled `oversubscribed_blocked`
- the row should also be read with the diagnostic flags, because this pattern can partly reflect a FES pre-allocation artefact rather than only physical land scarcity

## Why Equality Redistribution Is Not the Default

The current workflow constrains future nuclear build with national upper-bound
caps, equivalent to `sum(p_nom_candidates) <= national_headroom`. It does not
force `sum(p_nom_candidates) == target_mw`. A forced equality target would ask a
different question: where must the model place the FES total if that full total
has to be built? That is useful as a future sensitivity, but it would change the
default least-cost interpretation of the model.

## Worked Example: `wind_onshore @ Z11`

This is a typical `unchanged` row in the constrained HT35 run:

- `fes_spatial_cap_mw = 624.731437...`
- `land_cap_mw = 34357.29`
- `effective_total_cap_mw = 624.731437...`
- `live_existing_capacity_mw = 204.6`

Interpretation:

- the zonal land cap is much larger than the FES envelope
- land does not tighten the row
- the candidate remains available at the pre-policy cap
- the solve then builds positive `p_nom_opt_mw`

## Why the Pre-policy CSVs Can Match

`HT35_zonal_future_capacity_oversubscription.csv` and `HT35_zonal_constrained_future_capacity_oversubscription.csv` can match exactly because both are written before the policy stage applies technical-potential tightening.

The constrained/unconstrained difference appears later in:

- `resources/network/{scenario}.nc`
- `resources/network/{scenario}_solved.nc`
- `resources/analysis/{scenario}_location_constraint_report.csv`

For A/B interpretation, prefer the post-policy location report.
