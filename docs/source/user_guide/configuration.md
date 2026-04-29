# Configuration Reference

PyPSA-GB uses YAML configuration files to control scenario selection, subsystem behavior, and solve settings.

## Configuration Files

```text
config/
├── config.yaml       # Active scenarios + global overrides
├── scenarios.yaml    # Scenario definitions
├── defaults.yaml     # Default values inherited by all scenarios
└── clustering.yaml   # Clustering presets
```

Merged scenario configuration is built in `config/config_loader.py` in this order:

1. `defaults.yaml`
2. global overrides from `config.yaml`
3. scenario-specific values from `scenarios.yaml`

## `config.yaml`

`config/config.yaml` selects which scenarios run by default and can also carry global overrides.

Current syntax uses `run_scenarios`, not the older `scenarios:` list:

```yaml
run_scenarios:
  - HT35_zonal

solver:
  name: gurobi
  threads: 4
```

If you launch a single scenario directly, the command-line override usually takes precedence operationally:

```bash
snakemake resources/network/HT35_zonal_constrained_solved.nc --config scenario=HT35_zonal_constrained
```

This pattern is common for smoke tests, A/B comparisons, and documentation examples.

## `scenarios.yaml`

Each scenario inherits from `defaults.yaml` and overrides only what differs.

Example future zonal scenario:

```yaml
HT35_zonal_constrained:
  description: "FES 2025 Holistic Transition 2035 - Zonal Network with technical potential constraints"
  modelled_year: 2035
  FES_year: 2025
  FES_scenario: "Holistic Transition"
  renewables_year: 2020
  demand_year: 2015
  network_model: "Zonal"
  solve_period:
    enabled: true
    start: "2035-01-01 00:00"
    end: "2035-01-07 23:00"
  future_capacity_candidates:
    enabled: true
  technical_potential_constraints:
    enabled: true
```

## Key Config Surfaces

### Scenario selection and solve horizon

- `run_scenarios`: scenarios to run from `config/config.yaml`
- `solve_period`: optional sub-period solve window for faster future runs
- `network_model`: `ETYS`, `Reduced`, or `Zonal`

### Historical vs future routing

- `modelled_year <= 2024`
  - historical routing
  - DUKES, REPD, ESPENI
- `modelled_year > 2024`
  - future routing
  - FES-based capacity and demand inputs

### Future capacity candidates

`future_capacity_candidates` controls the extendable future-capacity workflow.

Important points:

- renewables use FES as the pre-policy build envelope
- large nuclear and SMR also use candidate logic in the thermal path
- candidates intentionally start with `p_nom = 0` and expand in solve up to `p_nom_max`

Relevant fields include:

- `enabled`
- `carriers`
- `capital_costs`
- `nuclear.workbook_paths`
- `nuclear.en6_sites_path`
- `nuclear.smr_anchors_path`
- `nuclear.smr_demand_weights`

### Technical-potential policy stage

`technical_potential_constraints` controls the post-assembly policy layer that tightens candidate `p_nom_max` before solve.

Important point:

- this setting changes the assembled network and post-policy analysis outputs
- it does **not** change the pre-policy oversubscription CSV

### Hydrogen network

`hydrogen.network` controls:

- `mode` such as full-network vs legacy copper-plate fallback
- topology input paths
- nearest-H2-node coupling behavior
- FES-based capacity-bound settings

### Marginal costs

`marginal_costs` controls:

- scenario carbon price assumptions
- explicit fuel-price overrides
- use of FES-derived future price inputs where configured

This stage still runs separately before storage integration.

### Nuclear metadata and siting

Relevant config surfaces:

- `nuclear.siting_constraints`
- `nuclear_technologies`
- `nuclear_to_x`

Current branch behavior:

- large nuclear uses EN-6-style site screening plus a configured per-site cap
- SMR uses repo-local anchors filtered by zonal SMR land availability
- heat extraction is scaffolded but not yet an active solved subsystem

## Common Configuration Patterns

### Future zonal A/B comparison

```yaml
HT35_zonal:
  future_capacity_candidates:
    enabled: true
  technical_potential_constraints:
    enabled: false

HT35_zonal_constrained:
  future_capacity_candidates:
    enabled: true
  technical_potential_constraints:
    enabled: true
```

This pair is useful for comparing:

- pre-policy candidate envelopes
- post-policy candidate tightening
- solved build differences

### Direct one-scenario runs

Use the same scenario definitions but override selection from the CLI:

```bash
python config/config_loader.py --scenario HT35_zonal_constrained
snakemake -n resources/network/HT35_zonal_constrained_solved.nc --config scenario=HT35_zonal_constrained
```

## Model and Network Options

### Network models

| Model | Use case |
|-------|----------|
| `ETYS` | full transmission detail |
| `Reduced` | faster national testing |
| `Zonal` | regional future-scenario and policy analysis |

### ETYS controls

- `etys.year`
- `etys_upgrades.enabled`
- `etys_upgrades.upgrade_year`
- `transmission.*`

These affect the full ETYS topology and upgrade application.

## Important Current-State Notes

- `carbon_budget` is currently a config surface only; it is not yet enforced by the active solve hook.
- `large_nuclear.max_per_site_mw` currently defaults to `3400 MW`; this is a repo assumption, not a documented source-backed capacity.
- `resources/generators/{scenario}_future_capacity_oversubscription.csv` is a pre-policy report.
- `resources/analysis/{scenario}_location_constraint_report.csv` is the post-policy, solved-aware interpretation report.

For the detailed siting formulas and label meanings, see {doc}`location_constraints`.
