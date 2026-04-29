# Scenario Design

This guide explains how to design scenarios against the **current** workflow, especially future scenarios that use candidate envelopes, policy-stage siting caps, hydrogen-network options, and nuclear-to-x scaffolding.

## Scenario Types

### Historical scenarios

Historical scenarios use actual system data rather than FES projections.

```yaml
Historical_2022:
  description: "Validation against 2022 outturn"
  modelled_year: 2022
  renewables_year: 2022
  demand_year: 2022
  network_model: "ETYS"
```

Typical data routing:

- thermal generation: DUKES
- renewables: REPD
- demand: ESPENI

Historical scenarios do not use the future-capacity candidate workflow.

### Future scenarios

Future scenarios use FES-based capacity and demand assumptions.

```yaml
HT35_zonal:
  description: "FES 2025 Holistic Transition 2035 - Zonal"
  modelled_year: 2035
  FES_year: 2025
  FES_scenario: "Holistic Transition"
  renewables_year: 2020
  demand_year: 2015
  network_model: "Zonal"
```

Typical future routing:

- future renewable capacity from FES
- future thermal and nuclear capacity from FES
- demand from scenario assumptions plus historical profile shapes
- optional future candidate envelopes for renewables and nuclear

## Recommended Future Scenario Patterns

### Baseline future candidate run

Use this when you want future candidates but no technical-potential policy tightening:

```yaml
HT35_zonal:
  future_capacity_candidates:
    enabled: true
  technical_potential_constraints:
    enabled: false
```

### Policy-constrained future run

Use this when you want the policy stage to tighten candidate caps before solve:

```yaml
HT35_zonal_constrained:
  future_capacity_candidates:
    enabled: true
  technical_potential_constraints:
    enabled: true
```

This is the standard A/B pair for checking whether land-derived caps change solved build.

### Full hydrogen-network run

Use this when you want the spatial hydrogen subsystem active:

```yaml
HT35_zonal_h2_full:
  network_model: "Zonal"
  hydrogen:
    network:
      mode: "full"
```

## FES Pathway Selection

### Holistic Transition

Balanced pathway used in many current smoke and A/B runs.

```yaml
FES_scenario: "Holistic Transition"
```

### Electric Engagement

Higher electrification pathway useful for stressing grid and flexibility assumptions.

```yaml
FES_scenario: "Electric Engagement"
```

### Hydrogen Evolution

Hydrogen-heavy pathway useful for sensitivity work on hydrogen build and coupled infrastructure.

```yaml
FES_scenario: "Hydrogen Evolution"
```

### Falling Short

Lower-progress stress or counterfactual pathway.

```yaml
FES_scenario: "Falling Short"
```

## Weather and Demand Year Selection

### `renewables_year`

This selects the weather year used to generate renewable profiles.

```yaml
renewables_year: 2020
```

Use multiple weather years for robustness when interpreting build decisions.

### `demand_year`

This selects the historical profile year used for demand shaping.

```yaml
demand_year: 2015
```

Future scenarios commonly mix future annual assumptions with historical profile shapes.

## Solve Period Selection

### Full year

```yaml
solve_period:
  enabled: false
```

### Representative or stress week

```yaml
solve_period:
  enabled: true
  start: "2035-01-01 00:00"
  end: "2035-01-07 23:00"
```

Important interpretation note:

- short solve windows can make candidate-cap changes economically weak even when the post-policy report shows real tightening
- in the current HT35 zonal A/B tests, the one-week winter solve mainly selects low-cost onshore wind candidates

## How Future Candidate Scenarios Behave

When `future_capacity_candidates.enabled: true`:

- FES acts as a pre-policy build envelope
- live existing capacity reduces available headroom
- land and siting policy can tighten candidate caps later
- candidates enter solve as extendable assets with `p_nom = 0` and positive `p_nom_max`

This means future scenarios now have two distinct interpretation stages:

1. candidate-envelope stage
2. post-policy tightened stage

For details, see {doc}`location_constraints`.

## Recommended A/B Interpretation Workflow

For `HT35_zonal` vs `HT35_zonal_constrained`, inspect:

1. `resources/generators/{scenario}_future_capacity_oversubscription.csv`
2. `resources/analysis/{scenario}_location_constraint_report.csv`
3. `resources/network/{scenario}_solved.nc`

Key point:

- the pre-policy oversubscription CSVs can match
- the meaningful constrained/unconstrained difference usually appears in the post-policy location report and solved network
