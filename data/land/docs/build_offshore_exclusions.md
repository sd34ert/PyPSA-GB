
data
- https://opendata-thecrownestate.opendata.arcgis.com/datasets/74571e6e4a7f42e4b4f097b4f740033b_0/explore?location=55.148833%2C-2.640495%2C6
- https://opendata-thecrownestate.opendata.arcgis.com/datasets/3b444ff9402049afa0bf7f63208334d7_0/explore?location=55.004281%2C-3.098019%2C5
- https://www.marinedataexchange.co.uk/details/TCE-2117/2020-everoze-the-crown-estate-research-characterisation-of-key-resource-areas-for-offshore-wind
# initial questions on existing codebase

the goal of @rules/land_constraints.smk is to determine the technical potential capacity for each of the energy technologies: onwind, offwind,solar, hydrogen electrolysers, hydrogen turbines, and nuclear SMRs. This allows the model to endogenously spatially distribute these technologies so to minimise total system cost. It only applies to future year scenarios & at this moment, the zonal network model.
New capacity can't:
1. be built where capacity already exists
2. in determined exclusion areas
3. exceed the technical potential capacity in each zone per technology

Therefore, the model will build new capacity of a technology in the cheapest zone up to its technical potential, and then building additional capacity in the second cheapest zone, etc. Simplifying assumptions are made for all other generation technologies, not relevant for this conversation.

Although, ETYS network upgrades cannot currently be applied to the zonal network model, it should be assumed that the model is developed so that etys network upgrades can be appied to the zonal network model.

Couple of questions first:
1. how does the existing codebase allocate future additional capacity for each technology to each zonal network bus?
2. how does the existing codebase allocate historical existing capacity for each technology, if relevant, to each zonal network bus?
3. does the existing codebase, for offshore wind, currently differentiate offshore wind farms which are connected to the transmission network via ac or dc connections? if so, is the ac or dc connection: - an explicit exogenous data parameter from FES or an alternative dataset e.g. REPD? - an assumption input to the model based on distance from shore? - derived by the model?
4. does the existing codebase, for offshore wind, currently differentiate offshore wind farms as fixed or floating technologies: - an explicit exogenous data parameter from FES or an alternative dataset? - an assumption input to the model based on water depth parameter from bathemetry data? - derived by the model?
5. does the existing codebase include costs of the transmission network & the planned updates or is it derived by the model reflected in node shadow prices based on line loading i.e. network congestion?
6. in atlite, what wind technology assumptions are used, if any, to calculate wind energy generation at each bus for each timestep?

## Q1: How does the codebase allocate **future** additional capacity to zonal buses?

**Two pathways depending on FES GSP field:**

- **GSP-mapped generators**: FES provides capacity per GSP location. Each GSP is fuzzy-matched to a network bus → capacity assigned directly to that single bus.
- **"Direct" transmission-connected generators** (e.g. `Direct(NGET)`, `Direct(SPTL)`, `Direct(SHETL)`): Capacity is distributed **proportionally to historical site capacities** across buses within the transmission region. Latitude bands define regions (SHETL >57°, SPTL 55.5-57°, NGET ≤55.5°).

**For thermals**, the distribution uses `build_historical_bus_distribution()` which weights by existing generator `p_nom` at each bus.

**p_nom is set directly** to the FES capacity value — no extendable generators, no spatial optimisation. This is exactly what the land constraints pipeline will change.

**Key files**: [integrate_renewable_generators.py](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/scripts/generators/integrate_renewable_generators.py) (lines 833-907, 1073-1320), [integrate_thermal_generators.py](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/scripts/generators/integrate_thermal_generators.py) (lines 70-128, 182-338)

## Q2: How does the codebase allocate **historical** existing capacity to zonal buses?

**Nearest-distance spatial mapping from REPD site coordinates:**

1. REPD sites have OSGB36 X/Y coordinates, converted to WGS84 lat/lon via pyproj
2. `map_sites_to_buses()` in [spatial_utils.py](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/scripts/utilities/spatial_utils.py) converts sites back to OSGB36, builds a sklearn `NearestNeighbors` index on bus locations, and assigns each site to its nearest bus (Euclidean in metres)
3. Year filtering: `operational_date ≤ modelled_year-12-31` ensures only sites operational by that year are included
4. For ETYS networks, an additional BMU-to-Node correction maps large generators (including offshore wind) to the correct 400kV buses

**Thermals** use DUKES 5.11 data (2004-2024) with similar spatial mapping. Bioenergy uses REPD sites uniformly scaled to match FES targets.

## Q3: Does the codebase differentiate offshore wind by AC vs DC connection?

**No, not currently.**

- The model uses a single `wind_offshore` carrier (aliased as `offwind`)
- FES provides aggregate "Offshore Wind" capacity — no AC/DC split
- REPD has no transmission type field
- ==**However**, `config/defaults.yaml` defines two separate carrier configs: `offwind-ac` (max 60m depth, 8 MW/km²) and `offwind-float` (max 1000m depth, 6 MW/km²), and the config validator supports both. This infrastructure exists but is **not yet wired into the active pipeline** — it's part of the land constraints work (Phase 8)==

The AC/DC distinction is **not derived by the model** — it would need to be an assumption based on distance from shore (planned: ≤50km = AC, >50km = DC) or an exogenous data parameter.

## Q4: Does the codebase differentiate offshore wind as fixed vs floating?

**No, not in the active pipeline.**

- Same single `wind_offshore` carrier for all offshore wind
- REPD does not have a foundation type field
- FES does not distinguish fixed vs floating
- A separate dataset exists at `data/renewables/future_offshore_sites/offshore_floating.xlsx` for pipeline floating projects, but it's not integrated
- The config defines `offwind-ac` (fixed, ≤60m) and `offwind-float` (floating, >60m) but these are **planned, not implemented**

Your notebook work with KRA data is the more sophisticated approach — KRAs inherently encode fixed vs floating suitability via Technology Group ratings, making bathymetry thresholding unnecessary.

## Q5: Does the codebase include transmission network costs?

**No. Transmission lines are capacity-constrained but cost-free.**

- Lines have `s_nom` (thermal capacity in MVA), `r`, `x`, `b` (electrical parameters) — but **no `capital_cost` or `marginal_cost`**
- Lines are **NOT extendable** (`s_nom_extendable` is never set to True)
- Network upgrades are **exogenous** from ETYS Appendix B 2023 Excel file, applied deterministically based on `modelled_year` (new circuits added, old ones removed/modified)
- **Shadow prices / dual variables are NOT captured** post-solve — so bus marginal prices reflecting congestion are not reported
- Interconnectors are Links with `marginal_cost=0.0`; economics come from external generators with European generation mix costs

**Implication for your offshore tiers**: The cost multipliers (0.7/0.9/1.2) you're proposing for offshore siting tiers would need to go on `capital_cost` of the generators themselves, not on transmission — because there are no transmission costs to modify. This is actually the right approach since the tiers capture total project cost differences (grid connection, consent, port infrastructure).

## Q6: What wind technology assumptions does atlite use?

**File**: [map_renewable_profiles.py](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/scripts/renewables/map_renewable_profiles.py)

**Onshore wind** — three turbine models available, but defaults to:

```python
"Vestas_V112_3MW"  # 3 MW reference turbine
```

Other options: `Vestas_V47_660kW` (0.66 MW), `Siemens_SWT_2300kW` (2.3 MW)

**Offshore wind** — three turbine models available, but defaults to:

```python
"NREL_ReferenceTurbine_5MW_offshore"  # 5 MW NREL reference
```

Other options: `Vestas_V112_3MW_offshore` (3 MW), `Vestas_V164_7MW_offshore` (7 MW)

**Critical limitation**: All offshore wind uses the **same single turbine model** regardless of fixed vs floating. There is no distinction in capacity factor profiles between foundation types. Modern floating platforms typically use larger turbines (12-15 MW), so this is a simplification that could be revisited — but it's outside the scope of `build_offshore_exclusions`.

**Solar**: CSi panel, latitude-optimal orientation.

All profiles use `capacity_factor_timeseries=True` at ERA5 temporal resolution, output as MW.

---

# FINAL CODE LOGIC
## Corrected understanding

`build_offshore_exclusions` has a **narrow, well-defined job**:

```
INPUT:  Raw marine vector/raster data
OUTPUT: Exclusion zones raster (GeoTIFF) — binary mask of where you CANNOT build
```

It does **not**:

- Calculate technical potential (that's `calculate_renewable_potential`)
- Calculate availability fractions (that's `build_renewable_availability_matrix`)
- Handle existing/committed capacity (that's `integrate_renewable_generators.py`)
- Apply cost tiers or KRA prioritisation (that's the optimiser via `apply_land_constraints`)
- Map offshore areas to onshore buses (that's `network_build.smk`)
- Need a `{scenario}` wildcard (physical geography doesn't change between scenarios)

The **downstream chain** is:

```
build_offshore_exclusions          → exclusion raster (run once)
         ↓
build_renewable_availability_matrix → availability per zone per tech
         ↓                            (KRA area - exclusions) / total zone area
calculate_renewable_potential      → p_nom_max per zone per tech
         ↓                            availability × capacity_density × area
integrate_renewable_generators     → adds existing + committed capacity as fixed generators
         ↓
apply_land_constraints             → sets p_nom_max = technical_potential - existing_capacity
         ↓
solve_network                      → optimiser builds new capacity ≤ p_nom_max, cheapest zones first
```

## So `build_offshore_exclusions` script logic is:

```
STAGE 1: Load & reproject all exclusion sources to EPSG:27700
  - MPAs (marine protected areas)
  - O&G fields + CCS & gas storage licence areas
  - Offshore marine mining licence areas
  - Shipping routes ≥ Q90 density
  - INTOG "no development" polygons

STAGE 2: Union all exclusion geometries into single layer

STAGE 3: Rasterize to binary mask at configured resolution
  - 1 = excluded, 0 = not excluded
  - Aligned to project reference grid (same as onshore foundation rasters)

STAGE 4: Write GeoTIFF output
```

The KRA polygons, existing leases, and technology group classifications are **not inputs to this rule** — they're consumed by `build_renewable_availability_matrix` downstream, which overlays the exclusion raster against the KRA eligible areas to calculate fractional availability.

Does that match your intent? If so, the rule and script are straightforward — essentially the same pattern as `build_protected_areas_raster` but for marine data.

---
## Code Logic Plan: `build_offshore_exclusions`

### Context Shift from Original Plan

Your original workflow plan (and task 8.1) envisioned a **raster-based** approach: read bathymetry → apply depth thresholds → produce two GeoTIFFs (`offshore_fixed_eligible.tif`, `offshore_float_eligible.tif`).

Your notebook exploration has fundamentally changed the approach to a **vector/polygon-based three-tier priority system** where: **DISAGREE**

- **KRAs already encode fixed vs floating** suitability (no bathymetry needed)
- **Cost multipliers** guide the optimiser rather than binary eligible/ineligible masks
- The output is **polygons with attributes**, not rasters

### Proposed Rule Signature

The rule inputs/outputs in [land_constraints.smk:427-440](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/rules/land_constraints.smk#L427-L440) need updating from the original spec. Here's what I'd propose:

```python
rule build_offshore_exclusions:
    """
    Build offshore wind siting layers using three-tier priority system.

    Tier 1: Existing capacity (Crown Estate Scotland leases, INTOG successful apps)
            → lowest cost multiplier (repowering/extension advantage)
    Tier 2: INTOG O&G Decarbonisation Areas of Search
            → medium cost multiplier (platform reuse, shorter cables)
    Tier 3: KRAs (Fixed ∪ Floating) minus exclusions minus Tier 1/2
            → highest cost multiplier (no infrastructure advantage)

    Exclusions = MPAs + shipping p90 + O&G/CCS fields + INTOG "no development"
                 + tidal/wave plan options

    Outputs are vector polygons with tier/technology attributes, NOT rasters.
    KRAs encode fixed vs floating suitability via Technology Group ratings,
    so bathymetry thresholding is unnecessary.

    Processing steps:
    1. Load exclusion_zones.gpkg (pre-assembled in notebook: 1,238 features)
    2. Load KRA polygons (fixed: 13 groups, floating: 6 groups)
    3. Load Tier 1 sources (Crown Estate Scotland wind leases, INTOG apps)
    4. Load INTOG AoS for Tier 2 (O&G decarbonisation areas)
    5. Classify: Tier 1 → Tier 2 → Tier 3 = (KRA ⊖ exclusions ⊖ T1 ⊖ T2)
    6. Tag each polygon: tier, technology (fixed/floating), area_km2
    7. Write combined output GeoPackage + summary CSV

    Transforms: 5 vector sources → tiered offshore siting GeoPackage + CSV
    """
    input:
        exclusions=f"{data_path}/land/marine/exclusion_zones.gpkg",
        kra_fixed=f"{data_path}/land/marine/raw/Fixed_Wind_KRA_(England26_NI)%2C_The_Crown_Estate.geojson",
        kra_floating=f"{data_path}/land/marine/raw/Floating_Wind_KRA_(England26_NI)%2C_The_Crown_Estate.geojson",
        ces_wind=f"{data_path}/land/marine/raw/Offshore_Wind_Crown_Estate_Scotland_1787939159095011122.gpkg",
        intog_apps=f"{data_path}/land/marine/raw/INTOG_Application_Areas_4321811103069388958.gpkg",
        smp_scotland=f"{data_path}/land/marine/smp_scotland.shp",
        zones=f"{data_path}/network/zonal/zones.geojson",
    output:
        siting=f"{resources_path}/land/offshore_siting_tiers.gpkg",
        summary=f"{resources_path}/land/offshore_siting_summary.csv",
    params:
        target_crs=27700,
        config=lambda wc: config.get('land_constraints', {}),
    message:
        "Building offshore wind siting tiers (3-tier priority system)"
    log:
        "logs/land/build_offshore_exclusions.log"
    benchmark:
        "benchmarks/land/build_offshore_exclusions.txt"
    resources:
        mem_mb=6000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_offshore_exclusions.py"
```

### Script Processing Logic

1. Call existing capacity from REPD

```
┌─────────────────────────────────────────────────────┐
│  STAGE 1: Load & Standardise (all → EPSG:27700)     │
│                                                     │
│  exclusions.gpkg ─────────→ exclusion_union (1 geom)│
│  KRA fixed (13 polys) ───→ kra_fixed_gdf            │
│  KRA floating (6 polys) ─→ kra_floating_gdf         │
│  CES wind leases ────────→ ces_operational_gdf      │
│  INTOG apps (13) ────────→ intog_gdf                │
│  SMP Scotland ───────────→ smp_gdf                  │
└─────────────┬───────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────┐
│  STAGE 2: Build Tier 1 — Existing/Consented         │
│                                                     │
│  Filter CES wind: Project_Phase in                  │
│    {Operational, Under Construction, Consented}     │
│  Filter INTOG apps: successful applications         │
│  tier1 = union(ces_operational, intog_successful)   │
│  Tag: tier=1, source={ces/intog}                    │
│  Tag technology: CES=check lease type,              │
│                  INTOG=all floating                  │
└─────────────┬───────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────┐
│  STAGE 3: Build Tier 2 — INTOG O&G Decarb AoS      │
│                                                     │
│  From SMP INTOG AoS data:                           │
│    Filter: category == "O&G decarbonisation"        │
│    Subtract: tier2 = intog_og_aos - tier1           │
│  Tag: tier=2, technology=floating                   │
└─────────────┬───────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────┐
│  STAGE 4: Build Tier 3 — KRA Residual               │
│                                                     │
│  kra_all = union(kra_fixed, kra_floating)           │
│  tier3 = kra_all - exclusion_union - tier1 - tier2  │
│                                                     │
│  Preserve technology attribute:                     │
│    - From kra_fixed polygons → technology="fixed"   │
│    - From kra_floating polygons → technology="float" │
│    - Overlap areas → technology="both"              │
│  Preserve Rating (Technology Group) attribute       │
└─────────────┬───────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────┐
│  STAGE 5: Spatial attribution to ESO zones          │
│                                                     │
│  For each tier polygon:                             │
│    Intersect with zones.geojson                     │
│    → zone assignment (may split across zones)       │
│    Calculate area_km2 per zone fragment             │
└─────────────┬───────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────┐
│  STAGE 6: Write outputs                             │
│                                                     │
│  offshore_siting_tiers.gpkg:                        │
│    Columns: tier, technology, zone, rating,         │
│             area_km2, source, geometry              │
│                                                     │
│  offshore_siting_summary.csv:                       │
│    Aggregated: zone × tier × technology →           │
│    total_area_km2, potential_mw (area × density)    │
└─────────────────────────────────────────────────────┘
```

### Key Design Decisions to Confirm

1. **No raster output** — The original plan produced GeoTIFFs. Your new approach is vector-based with cost tiers. The downstream `build_renewable_availability_matrix` rule will need updating too — it currently expects `offshore_fixed_eligible.tif` and `offshore_float_eligible.tif` as inputs. Instead it would consume the GeoPackage or the summary CSV.

2. **`exclusion_zones.gpkg` as a pre-processed input** — Your notebook already assembled this from 7 layers. The script treats it as a `data/` input (immutable). If you want the script to build it from raw sources instead, the input list and logic would expand significantly.

3. **Scotland coverage gap** — KRAs cover England & NI only. Scotland's offshore areas are covered by SMP wind plan options + Crown Estate Scotland leases. The Tier 3 logic needs to handle Scotland separately (SMP plan options as the equivalent of KRAs for Scottish waters).

4. **Cost multipliers** — Where do the tier cost multipliers (0.7, 0.9, 1.2) live? I'd suggest `config/defaults.yaml` under `land_constraints.offwind` so they're tuneable:

    ```yaml
    offwind:
      tier_cost_multipliers:
        existing: 0.7      # Tier 1
        intog_og: 0.9       # Tier 2
        kra_residual: 1.2   # Tier 3
    ```

5. **Downstream integration** — The summary CSV feeds into `calculate_renewable_potential` (task 8.3), which computes `p_nom_max` per zone per technology. The tier information + cost multipliers feed into `apply_land_constraints` (task 9) where they modify `capital_cost` on the extendable generators.
