# Architecture

Overview of the current PyPSA-GB branch architecture and the main design decisions that now shape future-scenario workflow behavior.

## High-Level Architecture

```{mermaid}
flowchart TB
    subgraph Config["Configuration Layer"]
        DEFAULTS["defaults.yaml"]
        MAIN["config.yaml"]
        SCENARIOS["scenarios.yaml"]
    end

    subgraph Workflow["Workflow Layer"]
        SNAKE["Snakefile / rules/*.smk"]
        POLICY["Post-assembly policy stage"]
    end

    subgraph Core["Execution Layer"]
        SCRIPTS["scripts/<domain>/..."]
        PYPSA["PyPSA networks"]
        SOLVE["solve hooks"]
    end

    subgraph Data["Data Layer"]
        INPUTS["data/ + resources/land/"]
        OUTPUTS["resources/"]
    end

    DEFAULTS --> SNAKE
    MAIN --> SNAKE
    SCENARIOS --> SNAKE
    INPUTS --> SCRIPTS
    SNAKE --> SCRIPTS
    SCRIPTS --> PYPSA
    PYPSA --> POLICY
    POLICY --> SOLVE
    SCRIPTS --> OUTPUTS
```

## Current Design Principles

### 1. Workflow-first assembly

PyPSA-GB is not a library-first architecture. The main contract is the Snakemake DAG and the network artifacts it produces.

### 2. Whole-system layering

This branch now assumes a whole-system view:

- network topology
- demand
- renewables
- thermal generation
- marginal costs
- storage
- nuclear metadata
- hydrogen subsystem
- interconnectors
- policy-stage mutations
- finalization, solve, and analysis

### 3. Future capacity as candidate envelopes

Future FES capacity is no longer always injected as fixed installed build. For active candidate carriers, FES is treated as an upper-bound envelope that can be tightened later by land and siting policy.

### 4. Post-assembly policy mutation

Technical-potential constraints are applied after the full network is assembled. This avoids losing subsystem context and makes constrained runs preserve storage, hydrogen, interconnectors, and marginal-cost work already added upstream.

## Configuration Architecture

Relevant files:

```text
config/
├── defaults.yaml
├── config.yaml
├── scenarios.yaml
└── config_loader.py
```

Merged scenario configuration is built as:

1. defaults
2. global overrides
3. scenario-specific overrides

This matters because candidate generation, hydrogen mode, and policy activation are all driven from the merged scenario object, not from raw YAML fragments.

## Workflow Architecture

The current baseline chain is:

```{mermaid}
flowchart LR
    BUILD["network_build"] --> DEMAND["demand"]
    DEMAND --> RENEW["renewables"]
    RENEW --> THERMAL["generators"]
    THERMAL --> MCOST["marginal costs"]
    MCOST --> STORAGE["storage"]
    STORAGE --> NUCLEAR["nuclear"]
    NUCLEAR --> H2["hydrogen"]
    H2 --> INTER["interconnectors"]
    INTER --> POLICY["policy"]
    POLICY --> FINALIZE["finalize"]
    FINALIZE --> SOLVE["solve"]
    SOLVE --> ANALYSIS["analysis"]
```

Supporting pipelines:

- `Snakefile_cutouts` for weather data
- `land_constraints.smk` for siting and technical-potential preprocessing
- optional clustering outputs for scenarios that enable clustering

## Key Architectural Shifts in the Current Branch

### Future candidates

- renewables build future candidate rows in the renewable integration stage
- large nuclear and SMR build future candidate rows in the thermal integration stage
- candidate rows are extendable and intentionally start with `p_nom = 0`

### Nuclear-to-x scaffolding

- nuclear metadata is added before hydrogen
- hydrogen coupling can recognize future SMR candidates
- heat extraction remains scaffolded rather than fully implemented

### Full hydrogen-network support

- hydrogen can run in full spatial-network mode using repo-local topology CSVs
- legacy copper-plate behavior still exists for fallback compatibility

### Post-policy reporting

- pre-policy candidate-envelope reporting and post-policy solved-aware reporting are intentionally separate
- this is now part of the architecture, not just an analysis convenience

## Location-Constraint Data Flow

```{mermaid}
flowchart LR
    FES["FES spatial capacity"] --> CAND["Candidate-envelope build"]
    LAND["technical_potential_Zonal.csv"] --> CAND
    EXIST["Live existing capacity"] --> CAND
    CAND --> PRE["future_capacity_oversubscription.csv"]
    CAND --> NET["assembled network"]
    NET --> POL["apply_technical_potential_constraints.py"]
    POL --> POST["{scenario}_network_constrained.nc"]
    POST --> SOLVED["{scenario}_solved.nc"]
    PRE --> REPORT["location_constraint_report.csv"]
    POST --> REPORT
    SOLVED --> REPORT
```

The detailed developer semantics for this flow live on {doc}`location_constraints`.

## Carrier Semantics

### Renewables

- zonal FES envelope
- zonal land caps
- no row-level land split

### Large nuclear

- workbook-derived national FES totals
- EN-6 site screening
- configured per-site cap used as `land_cap_mw`
- current `3400 MW` default is a repo assumption

### SMR

- workbook-derived national FES totals
- zonal SMR land caps
- repo-local demand anchors
- row-level land cap split across anchors

## Important Architectural Constraints

- `resources/network/{scenario}_network_constrained.nc` is the authoritative post-policy network artifact.
- `resources/generators/{scenario}_future_capacity_oversubscription.csv` is a pre-policy report and can match between constrained and unconstrained runs.
- `resources/analysis/{scenario}_location_constraint_report.csv` is the authoritative post-policy, solved-aware row-level explanation.
- Shared national FES caps for large nuclear and SMR are enforced at solve time, so local candidate caps alone do not determine total nuclear build.
