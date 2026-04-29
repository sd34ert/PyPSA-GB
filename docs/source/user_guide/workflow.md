# Snakemake Workflow

PyPSA-GB uses [Snakemake](https://snakemake.github.io/) to orchestrate a whole-system workflow. This page focuses on the current branch state rather than the older electricity-only pipeline.

## Main Workflow

The main workflow is defined in `Snakefile` and is assembled from modular rules in `rules/*.smk`.

The current baseline chain is:

```{mermaid}
flowchart LR
    BUILD["network_build"] --> DEMAND["demand"]
    DEMAND --> RENEW["renewables"]
    RENEW --> THERMAL["generators / thermal"]
    THERMAL --> MCOST["marginal costs"]
    MCOST --> STORAGE["storage"]
    STORAGE --> NUCLEAR["nuclear"]
    NUCLEAR --> HYDROGEN["hydrogen"]
    HYDROGEN --> INTER["interconnectors"]
    INTER --> POLICY["policy"]
    POLICY --> FINALIZE["finalize"]
    FINALIZE --> SOLVE["solve"]
    SOLVE --> ANALYSIS["analysis"]
```

Important notes:

- `renewables` and `generators` now build future capacity candidates for future scenarios when enabled.
- `generators` still applies marginal costs as a separate stage before storage.
- `nuclear` adds reactor metadata scaffolding for nuclear-to-x work.
- `hydrogen` can run in full spatial-network mode.
- `policy` is the authoritative post-assembly mutation stage for technical-potential constraints.
- `analysis` now includes the post-policy location-constraint report.

Optional paths:

- `Snakefile_cutouts` handles weather cutout acquisition and generation.
- `network_clustering.smk` still provides optional clustered-network outputs for scenarios that enable clustering.
- `land_constraints.smk` is a side pipeline that generates siting and technical-potential inputs used by candidate generation and policy.

## Two Workflows

### Main workflow

Use this for scenario assembly, solve, and analysis:

```bash
snakemake resources/network/HT35_zonal_constrained_solved.nc --config scenario=HT35_zonal_constrained
```

### Cutouts workflow

Use this if required atlite cutouts are missing:

```bash
snakemake -s Snakefile_cutouts --cores 1
```

The cutout workflow checks local cache first, then downloads from Zenodo for supported years, and finally falls back to ERA5 generation if needed.

## Rule Modules

| File | Purpose |
|------|---------|
| `network_build.smk` | Base ETYS, Reduced, and Zonal topology construction |
| `demand.smk` | Demand, disaggregation, and demand-side flexibility |
| `renewables.smk` | Renewable profile generation and renewable integration |
| `generators.smk` | Thermal integration plus future capacity candidates |
| `storage.smk` | Storage integration |
| `nuclear.smk` | Nuclear metadata scaffolding |
| `hydrogen.smk` | Hydrogen system integration, including full-network mode |
| `interconnectors.smk` | Cross-border links |
| `policy.smk` | Post-assembly technical-potential policy stage |
| `solve.smk` | Finalization and optimization |
| `analysis.smk` | Summaries, dashboards, notebooks, and location-constraint reporting |
| `land_constraints.smk` | Siting and technical-potential preprocessing |

## Intermediate Artifacts

The most important unsolved and solved outputs are:

```text
resources/network/{scenario}_network_demand_renewables_thermal_generators_costs.pkl
resources/network/{scenario}_network_demand_renewables_thermal_generators_storage_nuclear_hydrogen_interconnectors.nc
resources/network/{scenario}_network_constrained.nc
resources/network/{scenario}.nc
resources/network/{scenario}_solved.nc
```

Interpretation:

- `..._thermal_generators_costs.pkl`: generator-integrated network with marginal costs applied
- `..._interconnectors.nc`: the fully assembled pre-policy network
- `..._network_constrained.nc`: the post-policy network when technical-potential constraints are active
- `{scenario}.nc`: finalized clean pre-solve network
- `{scenario}_solved.nc`: optimized network with `p_nom_opt`, dispatch, flows, and costs

## Configuration Flow

Merged scenario configuration is built in `config/config_loader.py`:

1. `config/defaults.yaml`
2. global overrides from `config/config.yaml`
3. scenario-specific values from `config/scenarios.yaml`

Scenario execution selection comes from:

- `config/config.yaml` via `run_scenarios`
- or an explicit `--config scenario=<scenario_id>` override

## Current Siting/Policy Interpretation

For future candidate scenarios, the workflow currently separates:

- pre-policy candidate-envelope reporting
- post-policy candidate tightening
- solved build outcomes

Use:

- `resources/generators/{scenario}_future_capacity_oversubscription.csv`
  - pre-policy candidate-envelope report
- `resources/analysis/{scenario}_location_constraint_report.csv`
  - post-policy, solved-aware explanation of candidate outcomes

See {doc}`location_constraints` for the detailed formulas, labels, and worked examples.

## Common Commands

```bash
# Dry-run a scenario
snakemake -n resources/network/HT35_zonal_constrained_solved.nc --config scenario=HT35_zonal_constrained

# Solve one scenario directly
snakemake resources/network/HT35_zonal_constrained_solved.nc --config scenario=HT35_zonal_constrained

# Build only the post-policy location report
snakemake resources/analysis/HT35_zonal_constrained_location_constraint_report.csv --config scenario=HT35_zonal_constrained

# Run all active scenarios in config/config.yaml
snakemake --cores 4
```

## Troubleshooting

### A constrained and unconstrained oversubscription CSV look identical

That is expected when you are looking at:

- `resources/generators/{scenario}_future_capacity_oversubscription.csv`

This file is written before the policy stage. Use the post-policy report instead:

- `resources/analysis/{scenario}_location_constraint_report.csv`

### Finalized and solved networks look unchanged

Check whether the scenario actually enabled:

- `future_capacity_candidates.enabled`
- `technical_potential_constraints.enabled`

Also remember that a short solve window may make the policy effect economically small even when `p_nom_max` changed.

### A future candidate has `p_nom = 0`

That is expected for extendable candidates. Pre-solve candidates intentionally start at zero installed build and gain capacity through optimization up to `p_nom_max`.
