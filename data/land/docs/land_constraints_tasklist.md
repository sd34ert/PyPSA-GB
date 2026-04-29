# Land Constraints Implementation Task List

**Created**: 2026-02-08
**Updated**: 2026-03-29
**Source**: `docs/planning/workflow_plans/unified_land_constraints_workflow.md` (v3.0)
**Status**: Phase 6 (Nuclear) complete, Phase 7 (Hydrogen) updated for unified pipeline

> **2026-03-22 Refactor Note**: The UK LCM 2024 land cover raster (`land_cover_gb.tif`)
> was added to handle technology-specific habitat exclusions via
> `land_cover.exclusion_codes` and `land_cover.buffer_distances` in `defaults.yaml`.
>
> **2026-03-28 Reinstatement**: `green_belt_gb.tif`, `alc_bmv_gb.tif`, and
> `population_density_gb.tif` were **reinstated** as separate foundation rasters.
> Green Belt and ALC BMV are config-driven exclusion layers (enabled per technology).
> Population density feeds `build_nuclear_pop_criterion` (ONR demographic criterion).
> Tasks 3.3, 3.6, 3.7, 4.4, 4.7, 4.8 are active — scripts, rules, and tests exist.
>
> **2026-03-29 Architecture Refactor**: All onshore technologies now follow a unified
> three-step pipeline: (1) pixel-level exclusion → raster + availability CSV, (2)
> merge CSVs into availability matrix, (3) calculate technical potential. Phase 6
> nuclear completed with `build_nuclear_smr_exclusions` replacing `build_nuclear_eligibility`.
> Phase 7 hydrogen updated to follow the same pattern. New tasks 8.7-8.9 added for
> the unified aggregation steps.

**Successful completion:** no outstanding #tocode in this tasklist or [unified_land_constraints_workflow](PyPSA-GB/Planning/Workflow%20Plans/unified_land_constraints_workflow.md), all data verified & accurately documented, end-to-end testing & validation successfully completed, all documentation completed & added to docs/source/

---

## Codebase Conflicts & Required Adaptations

The workflow document was designed externally and has several conflicts with the actual codebase that must be resolved during implementation.

### Critical Conflicts

| # | Workflow Assumes | Codebase Reality | Resolution |
|---|-----------------|-----------------|------------|
| C1 | Zone shapes at `resources/network/{network_model}_regions.geojson` | GSP regions at `data/network/GSP/GSP_regions_27700_20250109.geojson` (OSGB36) and `data/network/GSP/GSP_regions_4326_20250109.geojson` (WGS84). Zonal regions at `data/network/zonal/zones.geojson`. No `resources/network/` zone files exist. | Use existing zone shape files from `data/network/`. Create a helper function that returns the correct zone file path based on `network_model` value (ETYS → GSP regions, Zonal → zonal regions). |
| C2 | EPSG:3035 (LAEA Europe) for all raster processing | Project standardizes on EPSG:27700 (OSGB36/British National Grid). EPSG:4326 for visualization. EPSG:3035 is used nowhere in the codebase. | Use EPSG:27700 for all raster processing. It is a projected CRS with metre units, suitable for area calculations within GB. Avoids cross-CRS confusion with existing spatial code in `scripts/utilities/spatial_utils.py`. |
| C3 | `{network_model}` as standalone wildcard in output paths | `{scenario}` is the primary wildcard. `network_model` is derived via `scenarios[wc.scenario].get('network_model', 'ETYS')`. `interconnectors.smk` `map_to_buses` already uses `{network_model}` as a standalone wildcard for outputs that vary only by network model. | Foundation rasters (no zone dependency) have no wildcard. Zone-dependent foundation outputs (`zone_statistics`, `scotland_mask`, `scotland_zones`) use `{network_model}` wildcard, following the `interconnectors.smk` `map_to_buses` precedent. Downstream rules (eligibility, availability) use `{scenario}` but derive `network_model` via `get_network_model()` for foundation file references. |
| C4 | Network input is `{scenario}_{network_model}_generators.nc` | Pipeline naming is `{scenario}_network_demand_renewables_thermal_generators_storage_hydrogen_interconnectors.nc`. Format is `.pkl` for intermediates, `.nc` only at final assembly stage (see `rules/STYLE_GUIDE.md` lines 196-213). | `apply_land_constraints` rule added to `solve.smk` (not a separate file). Insertion point: after interconnectors (or clustering) but before `finalize_network`. Uses `_get_pre_constraint_network()` helper to select correct input (clustered or unclustered). Output: `{scenario}_constrained.nc`. The rule always runs (passthrough when disabled), so `finalize_network` always reads `_constrained.nc` with no conditional logic. |
| C5 | `config.get('land_constraints', {})` for config access | Rules access scenario-specific config via `scenarios[wc.scenario].get(...)`. Global config accessed via `config` dict from Snakefile. Land constraints are logically global (not per-scenario), so `config.get(...)` is acceptable, but the pattern should be consistent with how hydrogen.smk accesses config. | Land constraint config is global (same constraints regardless of scenario). Use `config.get('land_constraints', {})` in rules. For nuclear/hydrogen siting, check if scenario-level override is needed. |
| C6 | No existing land/exclusion code | Confirmed: zero raster processing, no ExclusionContainer usage, no CORINE/Natura2000 code, no `resources/land/` or `data/land/` directories. However, extensive spatial infrastructure exists: `spatial_utils.py` (965 lines), coordinate transforms, site-to-bus mapping, land boundary checks. | Build entirely new. Can reuse `spatial_utils.py` for coordinate transforms and zone lookups. All raster processing is net-new. |
| C7 | `resources/land/`, `resources/generators/`, `resources/hydrogen/` output dirs | `resources/` directory is created at Snakemake runtime. None of these subdirs exist. Existing pattern: `resources/network/`, `resources/FES/`, `resources/renewable/`, `resources/demand/`. | Snakemake creates output directories automatically. No manual creation needed. Path pattern is consistent with existing conventions. |

### Template Compliance Issues

| # | Template Rule | Workflow Deviation | Fix Required |
|---|--------------|-------------------|--------------|
| T1 | **STYLE_GUIDE.md**: Use `f"{resources_path}/..."` path variables from Snakefile | Workflow hardcodes `"resources/land/..."` without f-string path variable | All rule paths must use `f"{resources_path}/land/..."` pattern |
| T2 | **STYLE_GUIDE.md**: Every rule needs `wildcard_constraints:` | Workflow rules have no wildcard constraints | Add `wildcard_constraints: scenario="[A-Za-z0-9_-]+"` to every rule with wildcards |
| T3 | **STYLE_GUIDE.md**: Every rule needs `message:` directive | Workflow rules have no message directives | Add descriptive `message:` to every rule |
| T4 | **STYLE_GUIDE.md**: Every rule needs `conda: "../envs/pypsa-gb.yaml"` | Workflow rules have no conda directive | Add `conda:` directive to every rule |
| T5 | **STYLE_GUIDE.md**: Don't redefine `resources_path`/`data_path` in rule files | Workflow doesn't address this, but `hydrogen.smk` lines 43-44 redefine them (anti-pattern) | Do NOT redefine path variables in new .smk files. Use variables from Snakefile. |
| T6 | **SCRIPT_PATTERNS.md**: Scripts need `setup_logging()`, stage timing, `main()`, `if __name__ == "__main__":` guard | Workflow specifies processing logic but not script structure | Every script must follow the full script template from SCRIPT_PATTERNS.md |
| T7 | **SCRIPT_PATTERNS.md**: Scripts need `snk = globals().get('snakemake')` with standalone fallback | Workflow doesn't address standalone execution | Every script must handle both Snakemake and standalone execution |
| T8 | **SCRIPT_PATTERNS.md**: `sys.path.insert(0, str(project_root))` before project imports | Not mentioned in workflow | All scripts in `scripts/land/`, `scripts/generators/`, `scripts/hydrogen/` need path setup |
| T9 | **CONFIG_PATTERNS.md**: New config sections need: defaults.yaml + scenarios.yaml docs + config_loader.py validation | Workflow proposes config schema but doesn't address the 3-step config integration pattern | Must add defaults, document in scenarios.yaml, and add validation for `land_constraints`, `nuclear.siting_constraints`, `hydrogen.siting_constraints` |
| T10 | **DATA_PATTERNS.md**: Track data provenance with `data_source` column | Workflow CSV outputs don't include provenance columns | Add `data_source` column to all CSV outputs where data is merged from multiple sources |
| T11 | **TESTING_PATTERNS.md**: 3-tier tests (smoke, unit, integration) with markers | Workflow doesn't address testing | Must create tests following the 3-tier pattern |
| T12 | **VALIDATION_PATTERNS.md**: Input validation, informative errors, `validate_dataframe()` pattern | Workflow doesn't specify validation | Every script must validate inputs before expensive processing |
| T13 | **SNAKEMAKE_PATTERNS.md**: Conditional inputs use `unpack()` with input functions | Workflow uses `**get_land_constraint_inputs` (dict unpacking) | Use `unpack(get_land_constraint_inputs)` pattern per template |
| T14 | **STYLE_GUIDE.md**: Log paths follow `logs/{module_name}/{rule_name}_{wildcard}.log` | Workflow uses `logs/land/build_protected_areas_raster.log` (no wildcard for non-wildcard rules, which is fine) | Ensure wildcard-dependent rules include wildcards in log paths. Non-wildcard rules are fine without. |

### Dependency Status

| Package | Required By | In Environment? | Notes |
|---------|------------|-----------------|-------|
| geopandas | All spatial scripts | Yes | Used extensively in existing code |
| rasterio | All raster scripts | **Check needed** | Not imported anywhere currently |
| rioxarray | Raster/xarray bridge | **Check needed** | Not imported anywhere currently |
| xarray | Availability matrix | Yes | Used in `map_renewable_profiles.py` |
| atlite | ExclusionContainer | Yes | Used in `prepare_cutouts.py`, `map_renewable_profiles.py` |
| shapely | Buffer operations | Yes | Used in ETYS_network.py, spatial_utils.py |
| fiona | Vector I/O | Yes | Dependency of geopandas |
| pyproj | CRS transforms | Yes | Used in spatial_utils.py, ETYS_network.py |

---

## Phase 1: Data Acquisition

- [ ] TODO: download & process green_belt_scotland.gpkg & green_belt_wales.gpkg, update data_sourcces_land.md & land_constraints_tasklist.md (Issue D4: Green Belt Scotland — missing on disk)
- [x] DONE: E&W census population density data sourced — Census 2021 TS006 at OA level (188,880 rows). Raw: `data/land/societal/raw/TS006-2021-4-filtered-2026-02-28T08_12_29Z.csv`. Processed: `data/land/societal/output_areas_ew.csv`
- [ ] TODO: rename/copy raw TS006 CSV to `data/land/societal/output_areas_ew.csv` with standardised column names

> **Goal**: Obtain and place every raw dataset file referenced by the workflow.
> **Finish before**: Phase 2.

### 1.1 Create raw data directory structure — DONE

- **Time**: 15min
- **Action**: Create the following directory structure: `data/land/environment/`, `data/land/environment/raw/`, `data/land/hazards/`, `data/land/hazards/raw/`, `data/land/marine/`, `data/land/marine/raw/`, `data/land/societal/`, `data/land/societal/raw/`
- **File(s)**: Directory creation only
- **Done when**: `ls data/land/environment data/land/hazards data/land/marine data/land/societal` succeeds with no errors

### 1.2 Document data sources with download URLs — DONE

- **Time**: 2hr
- **Action**: Create `data/land/DATA_SOURCES.md` documenting every dataset listed in workflow Section 2 ("Data Source Audit"). For each dataset, record: name, source URL, licence, format, expected filename, and download instructions. Cover all 25+ datasets across shared, nuclear-specific, and technology-specific categories.
- **File(s)**: `data/land/DATA_SOURCES.md` (new)
- **Done when**: Every dataset from workflow Table 2 has a documented source URL and download instruction. A second person could follow the doc and obtain all files.

### 1.3 Download/acquire protected area datasets (SAC, SPA, Ramsar) — DONE

- **Time**: 1hr
- **Action**: Download SAC, SPA, and Ramsar datasets from Natural England / NatureScot / NRW open data portals. Save as `data/land/environment/gb_sac.gpkg`, `gb_spa.gpkg`, `gb_ramsar.gpkg`.
- **File(s)**: `data/land/environment/gb_sac.gpkg`, `gb_spa.gpkg`, `gb_ramsar.gpkg`
- **Done when**: Each file loads in geopandas (`gpd.read_file(path)`) without error and contains polygon geometries covering GB.

### 1.4 Download/acquire SSSI datasets (England, Scotland, Wales) — DONE

- **Time**: 1hr
- **Action**: Download SSSI boundaries from Natural England, NatureScot, and NRW. Save as `data/land/environment/sssi_england.gpkg`, `sssi_scotland.gpkg`, `sssi_wales.gpkg`.
- **File(s)**: `data/land/environment/sssi_england.gpkg`, `sssi_scotland.gpkg`, `sssi_wales.gpkg`
- **Done when**: Each file loads in geopandas with polygon geometries.

### 1.5 Download/acquire AONB, NSA, and National Parks datasets — DONE

- **Time**: 1hr
- **Action**: Download AONB (England & Wales), National Scenic Areas (Scotland), and National Parks (England, Scotland, Wales) boundaries. Save to `data/land/environment/` with filenames matching workflow Section 7.
- **File(s)**: `data/land/environment/aonb_england.gpkg`, `aonb_wales.gpkg`, `nsa_scotland.gpkg`, `national_parks_england.gpkg`, `national_parks_wales.gpkg`, `national_parks_scotland.gpkg`
- **Done when**: All 6 files load in geopandas.

### 1.6 Download/acquire UK Land Cover Map 2024 — DONE

- **Time**: 30min
- **Action**: Download UK Land Cover Map 2024 (UKCEH) GeoTIFF. Save as `data/land/environment/uk_lcm_2024.tif`. Note: may require UKCEH account/licence.
- **File(s)**: `data/land/environment/uk_lcm_2024.tif`
- **Done when**: File opens with `rasterio.open()` and has integer land cover class values.

### 1.7 Download/acquire Census 2021 population data and Output Area boundaries — DONE

- **Time**: 1hr
- **Action**: Download Census 2021 Output Area boundary files for England & Wales (ONS) and Scotland (NRS). Scotland GPKG has population counts embedded (`Popcount`, `HHcount`, `sqkm` columns; 46,363 OAs). England & Wales GPKG contains boundaries only (`OA21CD`, coordinates, `GlobalID`; 188,880 OAs) — population density (people/km²) is sourced from Census 2021 TS006 at OA level and joined via `OA21CD` → `Output Areas Code`. Save to `data/land/societal/`.
- **File(s)**: `data/land/societal/output_areas_ew.gpkg`, `data/land/societal/output_areas_scotland.gpkg`, `data/land/societal/output_areas_ew.csv` (E&W population density from ONS NOMIS TS006, 188,880 rows; raw: `data/land/societal/raw/TS006-2021-4-filtered-2026-02-28T08_12_29Z.csv`)
- **Done when**: Scotland GPKG loads with `Popcount` column. E&W GPKG loads with `OA21CD` column. E&W density CSV has 188,880 rows matching E&W GPKG features. Join on `OA21CD` → `Output Areas Code` produces zero nulls.

### 1.8 Download/acquire flooding risk datasets (EA, SEPA, NRW) — DONE

- **Time**: 1hr
- **Action**: Download flood risk zone boundaries from Environment Agency (England), SEPA (Scotland — river, surface water, coastal), NRW (Wales — rivers & seas, surface water). Save to `data/land/hazards/`.
- **File(s)**: `data/land/hazards/ea_flood_zones.gpkg`, `data/land/hazards/sepa_flood_zones.gpkg`, `data/land/hazards/sepa_flood_zones_surface.gpkg`, `data/land/hazards/sepa_flood_zones_coastal.gpkg`, `data/land/hazards/nrw_flood_zones.gpkg`, `data/land/hazards/nrw_flood_zones_surface.gpkg`
- **Done when**: All 6 files load in geopandas with polygon/multipolygon geometries.

### 1.9 Download/acquire Source Protection Zones (England, Wales, Scotland) — DONE

- **Time**: 1hr
- **Action**: Download SPZ boundaries from DEFRA/EA (England), NRW (Wales), and SEPA DWPA (Scotland — 4 files: catchments, groundwaters, lochs, rivers). Save to `data/land/environment/`.
- **File(s)**: `data/land/environment/defra_source_protection_zones.gpkg`, `data/land/environment/nrw_source_protection_zones.gpkg`, `data/land/environment/sepa_dwpa_catchments.gpkg`, `data/land/environment/sepa_dwpa_groundwaters.gpkg`, `data/land/environment/sepa_dwpa_lochs.gpkg`, `data/land/environment/sepa_dwpa_rivers.gpkg`
- **Done when**: All 6 files load in geopandas with SPZ/DWPA zone classification attributes.

### 1.10 Download/acquire offshore datasets (GEBCO, Crown Estate, SMP, marine protected) — DONE

- **Time**: 1hr
- **Action**: Download GEBCO 2025 bathymetry (GeoTIFF), Crown Estate offshore leasing regions (England & Wales), Sectoral Marine Plan regions (Scotland), and Marine Protected Areas (Scotland). Save to `data/land/marine/`.
- **File(s)**: `data/land/marine/gebco_2025.tif`, `data/land/marine/offwind_ew.shp`, `data/land/marine/smp_scotland.shp`, `data/land/marine/marine_protected_scotland.gpkg`
- **Done when**: Bathymetry opens with rasterio and has depth values. Vector files load in geopandas.

### 1.11 Download/acquire airports dataset — DONE

- **Time**: 30min
- **Action**: Download UK airports locations (CAA or OS Open Data). Save as `data/land/hazards/airports_gb.gpkg`.
- **File(s)**: `data/land/hazards/airports_gb.gpkg`
- **Done when**: File loads in geopandas with point geometries and airport name/type attributes.

### 1.12 Download/acquire NCERM 2024 coastal erosion dataset — DONE

- **Time**: 30min
- **Action**: Download National Coastal Erosion Risk Mapping (NCERM) 2024 GeoPackage from Environment Agency Open Data. Contains 14 layers representing erosion extents under different SMP/NFI policy scenarios, climate change percentiles, and time horizons. 3 layers used: `NCERM_SMP_2105_70CC` (SMP 2105 70th %ile CC from UKCP18 RCP8.5), `NCERM_Ground_Instability_Zone` (historical), `NCERM_Ground_Instability_Recession` (predicted). Data already in EPSG:27700. ~22k features total across 3 layers. MultiPolygon Z geometries.
- **Source**: https://environment.data.gov.uk/dataset/9fede91f-5acd-4fd2-9bd8-98153fa3c2ff
- **File(s)**: `data/land/hazards/coastal_erosion_uk_2024.gpkg`
- **Done when**: File loads in geopandas. All 3 target layers accessible via `gpd.read_file(path, layer=name)`. CRS is EPSG:27700.
- **Validation notebook**: `notebooks/coastal_change.ipynb` — exploratory analysis comparing SMP vs NFI scenarios, climate change percentiles, and per-zone erosion area.

### 1.13 Compile nuclear-specific datasets — DONE

- **Time**: 2hr
- **Action**: Create/download: EN-6 designated nuclear sites (manual CSV from planning policy), water availability zones (manual compilation from EA/NRW/SEPA), UKCEH reservoir locations, National Gas transmission sites, BGS seismic hazard zones, NSTA onshore wells, deep geothermal areas. Save to `data/land/hazards/` (hazard datasets) and `data/generators/` (EN-6) and `data/water/` (water availability).
- **File(s)**: `data/generators/en6_designated_sites.csv`, `data/water/water_availability_zones.csv`, `data/land/hazards/ukceh_reservoirs.csv`, `data/land/hazards/national_gas_sites.shp`, `data/land/hazards/bgs_seismic_hazard.gpkg`, `data/land/hazards/nsta_onshore_wells.gpkg`, `data/land/hazards/deep_geothermal.gpkg`
- **Done when**: All 7 files exist and load without error. EN-6 CSV has columns: `site_name`, `lat`, `lon`, `max_capacity_mw`.

---

## Phase 2: Environment & Config Setup

> **Goal**: Install missing dependencies, add config sections, create directory scaffolding.
> **Finish before**: Phase 3.

### 2.1 Check and add missing Python dependencies

- **Time**: 30min
- **Action**: Check `environment.yaml` (or equivalent conda/pip env file) for `rasterio` and `rioxarray`. Add them if missing. These are the only packages not already present that the workflow requires.
- **File(s)**: `environment.yaml` or `envs/pypsa-gb.yaml`
- **Done when**: `python -c "import rasterio; import rioxarray; print('OK')"` succeeds in the project environment.

### 2.2 Add `land_constraints` section to `config/defaults.yaml` -  ADDED
#tocode update once data verified against policy docs
#tocode add site restrictions for other generators as per simplifying assumptions

- **Time**: 30min
- **Action**: Add the `land_constraints:` section from workflow Section 6 to `config/defaults.yaml`, with `enabled: false` as default. Adapt CRS from 3035 to 27700 per conflict C2. Use the project's standard config comment formatting (box drawing characters `═` and `─`).
- **File(s)**: `config/defaults.yaml`
- **Done when**: `python -c "import yaml; d=yaml.safe_load(open('config/defaults.yaml')); assert d['land_constraints']['enabled'] == False; print('OK')"` passes. Section includes `foundation`, `onwind`, `offwind-ac`, `offwind-float`, `solar` subsections. Each technology has `protected_tiers` (boolean per tier) instead of `protected_buffers`.

### 2.3 Add `nuclear.siting_constraints` section to `config/defaults.yaml` - ADDED
#tocode add EN-6 sites
#tocode add ccs clusters
#tocode add site restrictions for other generators as per simplifying assumptions

- **Time**: 15min
- **Action**: Add the `nuclear.siting_constraints` section from workflow Section 6 to `config/defaults.yaml` under the existing `nuclear:` key (or create it if absent), with `enabled: false`.
- **File(s)**: `config/defaults.yaml`
- **Done when**: `config['nuclear']['siting_constraints']['enabled']` resolves to `False` when defaults.yaml is loaded.

### 2.4 Add `hydrogen.siting_constraints` section to `config/defaults.yaml` - ADDED
#tocode update H2 turbine site restrictions once verified against policy docs

- **Time**: 15min
- **Action**: Add the `hydrogen.siting_constraints` section from workflow Section 6 to `config/defaults.yaml`, with `enabled: false`.
- **File(s)**: `config/defaults.yaml`
- **Done when**: `config['hydrogen']['siting_constraints']['enabled']` resolves to `False` when defaults.yaml is loaded.

### 2.5 Add `scotland.zones` section to `config/defaults.yaml` - ADDED
#tocode once implemented ssep_zonal network update name of scotland zones

- **Time**: 15min
- **Action**: Add `scotland: zones: ["North Scotland", "South Scotland"]` to `config/defaults.yaml`. Verify these zone names match the actual zone names in the GSP regions GeoJSON (check column values).
- **File(s)**: `config/defaults.yaml`
- **Done when**: Config loads and `config['scotland']['zones']` is a list of strings. Zone names verified against actual GeoJSON attributes.

### 2.5b Add `offshore_connection`, `offwind_tg_multipliers`, `offwind_cost_tiers` to `config/defaults.yaml`
#tocode add once implementing Phase 8 offshore wind spatial distribution

- **Time**: 30min
- **Action**: Add three new subsections under `land_constraints:` in `config/defaults.yaml`:
  - `offshore_connection:` — AC/DC threshold (50km), cable costs (£M/MW/km), substation/converter costs, loss parameters
  - `offwind_tg_multipliers:` — per-TG cost multipliers for fixed (TG-1 to TG-7B) and floating (TG-1 to TG-6)
  - `offwind_cost_tiers:` — maps TGs to named cost tiers (F1-F3b for fixed, FL1-FL3b for floating) for generator grouping
  See workflow doc Section 6 for full schema.
- **File(s)**: `config/defaults.yaml`
- **Done when**: Config loads and `config['land_constraints']['offshore_connection']['ac_threshold_km']` resolves to 50. TG multipliers and cost tiers match the tables in the workflow doc.

### 2.5c Add `offwind-ac` and `offwind-float` carrier definitions
#tocode add once implementing Phase 8 offshore wind spatial distribution

- **Time**: 15min
- **Action**: Add two new carrier entries to `scripts/utilities/carrier_definitions.py`: `offwind-ac` (fixed-bottom, colour #6BAED6) and `offwind-float` (floating, colour #4292C6). Both have `co2_emissions: 0.0`.
- **File(s)**: `scripts/utilities/carrier_definitions.py`
- **Done when**: Both carriers are importable and have correct attributes.

### 2.6 Document new config sections in `config/scenarios.yaml` - #todo
#todo document new config sections & update data_sources_land after finished data verifying against policy docs

- **Time**: 15min
- **Action**: Add commented-out template blocks in `config/scenarios.yaml` showing how to enable land constraints, nuclear siting, and hydrogen siting for a scenario. Follow the existing documentation comment pattern.
- **File(s)**: `config/scenarios.yaml`
- **Done when**: Comments clearly show how to override `land_constraints.enabled: true`, `nuclear.siting_constraints.enabled: true`, and `hydrogen.siting_constraints.enabled: true`.

### 2.7 Add config validation for land constraint sections in `config/config_loader.py`
#tocode updates tests/workflow/test_future_scenario_pipeline.py & test_scenario_validation.py

- **Time**: 1hr
- **Action**: Add validation logic to `validate_scenario()` in `config/config_loader.py` for the three new config sections. Validate: when enabled, required sub-keys exist; numeric values are positive; CRS is a valid EPSG code; land cover codes are lists of integers. Follow the existing validation pattern (return list of error strings).
- **File(s)**: `config/config_loader.py`
- **Done when**: `config_loader.py --validate` passes with new defaults. Intentionally invalid config (e.g., negative buffer distance) triggers a validation error.

#### Three private helper validators
summary of what was added to [config_loader.py](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/config/config_loader.py):

1. **`_validate_land_constraints()`** — validates when `land_constraints.enabled: true`:
    - Cross-checks: rejects historical scenarios (modelled_year <= 2024), rejects unsupported network models (case-insensitive comparison against `supported_network_models`)
    - `min_modelled_year`: must be int > 2024
    - `supported_network_models`: must be a non-empty list of valid network model strings
    - `foundation`: resolution must be positive, CRS values must be positive integer EPSG codes
    - `protected_tiers`: all tier values must be boolean (true/false)
    - `onwind`: land_cover_codes must be list of ints, urban_buffer non-negative, capacity_density positive
    - `offshore_connection`: ac_threshold_km positive, all cable/substation costs non-negative, loss parameters in [0, 1]
    - `offwind_tg_multipliers`: all values must be positive numbers >= 1.0, all expected TG keys present
    - `offwind_cost_tiers`: all tier lists non-empty, all referenced TGs exist in multipliers
    - `offwind-ac` / `offwind-float`: max_depth positive, min_shore_distance non-negative, capacity_density positive
    - `solar`: land_cover_codes list of ints, capacity_density positive
2. **`_validate_nuclear_siting()`** — validates when `nuclear.siting_constraints.enabled: true`:
    - Cross-checks: rejects historical scenarios
    - `large_nuclear.max_per_site_mw`: must be positive
    - `smr` (when enabled): all buffer distances positive, population_threshold positive, fraction fields (protected_area, flood_risk, groundwater_spz) in [0, 1], cooling_options must be a non-empty list from `{coastal, major_river, dry_cooling}`, dry_cooling_efficiency_penalty in [0, 1]
3. **`_validate_hydrogen_siting()`** — validates when `hydrogen.siting_constraints.enabled: true`:
    - Cross-checks: rejects historical scenarios
    - Validates both `electrolysis` and `h2_turbine` sub-sections with the same pattern: buffer distances positive, population_threshold positive, fraction fields in [0, 1]

#### Three shared utility validators

`_validate_positive()`, `_validate_non_negative()`, and `_validate_fraction()` — reusable validators that return `list[str]` error messages, consistent with the existing `validate_scenario()` return pattern.
#### Integration

All three helpers are called from `validate_scenario()` at [config_loader.py:487-489](vscode-webview://09bqo44edvta21m0tp13i6q0sr7r0h3sj8oofq6d1r3vt0bmld6i/config/config_loader.py#L487-L489). When all sections are disabled (the default), no additional validation runs — zero impact on existing scenarios.
### 2.8 Create `scripts/land/__init__.py` and `scripts/generators/__init__.py`

- **Time**: 15min
- **Action**: Create empty `__init__.py` files in `scripts/land/` and `scripts/generators/`. Note: `scripts/hydrogen/` already exists (contains `add_hydrogen_system.py`), check if `__init__.py` exists there too — create if missing.
- **File(s)**: `scripts/land/__init__.py`, `scripts/generators/__init__.py`, `scripts/hydrogen/__init__.py` (if missing)
- **Done when**: `python -c "import scripts.land; import scripts.nuclear; import scripts.hydrogen"` succeeds from project root.

### 2.9 Create zone shape helper function - SKIPPED, MAY BE NEEDED FOR BUILDING `network_build.smk`

- **Time**: 1hr
- **Action**: Add a function `get_zone_shapes_path(network_model: str) -> str` to `scripts/utilities/spatial_utils.py` that returns the correct zone boundaries file path for a given network model. ETYS → `data/network/GSP/GSP_regions_27700_20250109.geojson`, Zonal → `data/network/zonal/zones.geojson`. This resolves conflict C1.
- **File(s)**: `scripts/utilities/spatial_utils.py`
- **Done when**: Function returns correct path for "ETYS" and "Zonal" inputs. Raises `ValueError` for unknown network models. Unit test passes.

#### No, this helper function is NOT required for the land constraints workflow but may be needed for network_build.smk

`get_zone_shapes_path()` would only be useful in:

1. **`network_build.smk`** — if a rule needs to read the **raw** zone data:
    ```python
    # Inside a script called by network_build.smk
    from scripts.utilities.spatial_utils import get_zone_shapes_path

    raw_zones_path = get_zone_shapes_path(network_model)  # Returns data/network/GSP/... or data/network/zonal/...
    zones = load_and_reproject_vector(raw_zones_path, target_crs=27700)
    ```

2. **Standalone testing/validation scripts** that don't run under Snakemake:
    ```python
    # Manual testing outside Snakemake
    zones_path = get_zone_shapes_path("ETYS")
    zones = load_zone_shapes(zones_path, target_crs=27700)
    ```

3. **Config-driven scripts** where the script needs to programmatically determine which zones to use based on config values (but this is bad design — Snakemake should handle this).

**Skip implementing `get_zone_shapes_path()`** for land constraints. If you later find you need it (e.g., when building `network_build.smk` rules that process raw zone data), add it then.
**If you do implement it**, it belongs in `spatial_utils.py` (not `process_land_data.py`) because it's about **path conventions**, not **data processing operations**.

### 2.10 Write utility script, scripts/utilities/land_utils.py - DONE
#tocode 1 outstanding TODO in land_utils.py (line 652): confirm zonal.geojson & ssep_zones.geojson zone name column

This is the **shared toolbox** all foundation scripts import from. Each function encapsulates a specific operation:
#### Vector I/O & Manipulation

**`load_and_reproject_vector(path, target_crs)`**

- **What it does**: Opens a vector file (gpkg/shp), checks its current CRS, reprojects to target CRS
- **Logic flow**:
    1. Use `geopandas.read_file(path)` to load
    2. Check `gdf.crs` — if None, raise error (undefined CRS is unusable)
    3. If `gdf.crs != target_crs`, call `gdf.to_crs(target_crs)`
    4. Return reprojected GeoDataFrame
- **Why**: Every dataset arrives in different CRS (OSGB36, WGS84, etc.) — standardise to EPSG:27700 (OSGB) for processing

**`merge_national_datasets(paths, target_crs)`**
function

- **What it does**: Combines England/Scotland/Wales files into single GB dataset
- **Logic flow**:
    1. For each path in `paths`:
        - Load with `load_and_reproject_vector(path, target_crs)`
    2. Concatenate all GeoDataFrames: `gpd.pd.concat(gdfs, ignore_index=True)`
    3. Return merged GeoDataFrame
- **Example use**: SSSI has 3 files (sssi_england.gpkg, sssi_scotland.gpkg, sssi_wales.gpkg) → merge into single GB layer

**`dissolve_overlaps(gdf)`**

- **What it does**: Union overlapping polygons within a layer so each pixel is counted once
- **Logic flow**:
    1. Call `gdf.dissolve()` to merge all geometries into single MultiPolygon
    2. Explode back to individual polygons if needed
    3. Return dissolved GeoDataFrame
- **Why**: Protected areas overlap (e.g., SAC + SPA on same site). Without dissolving, pixel would be double-counted as "excluded twice".

**`buffer_geometries(gdf, distance_m)`**

- **What it does**: Applies spatial buffer to create exclusion zones
- **Logic flow**:
    1. Check CRS units — if projected (metres), use distance directly; if geographic (degrees), raise error
    2. `gdf['geometry'] = gdf.geometry.buffer(distance_m)`
    3. Return buffered GeoDataFrame
- **Example**: Nuclear constraint applies 500m buffer to Natura2000 sites

#### Rasterisation

**`get_gb_canonical_bounds(crs="EPSG:27700")`**

- **What it does**: Returns the canonical GB bounding box `(0, 0, 700_000, 1_300_000)` in EPSG:27700
- **Why critical**: Single source of truth for the raster grid extent. All foundation scripts must use these bounds so every output shares identical extent for pixel-aligned stacking.

**`create_reference_grid(bounds=None, resolution=100, crs="EPSG:27700")`**

- **What it does**: Creates the **template grid** that ALL rasters must match
- **Logic flow**:
    1. If bounds is None, call `get_gb_canonical_bounds(crs)` to get canonical GB bounds
    2. Extract bounds: `(xmin, ymin, xmax, ymax)`
    3. Calculate dimensions:

```python
     width = int((xmax - xmin) / resolution)
     height = int((ymax - ymin) / resolution)
```

3. Create affine transform linking pixel coordinates to real-world coordinates:

```python
     transform = rasterio.Affine.translation(xmin, ymax) * rasterio.Affine.scale(resolution, -resolution)
```

4. Return `(width, height, transform, crs)` as template

- **Why critical**: This ensures every raster has **identical pixel alignment**. Without it, stacking layers later requires resampling (introduces errors).

**`rasterize_vector(gdf, template, burn_value, dtype)`**

- **What it does**: Burns vector geometries into raster grid (presence/absence)
- **Logic flow**:
    1. Unpack template: `width, height, transform, crs`
    2. Create empty array: `np.zeros((height, width), dtype=dtype)`
    3. Use `rasterio.features.rasterize()` to burn geometries:

```python
     rasterized = features.rasterize(
         shapes=[(geom, burn_value) for geom in gdf.geometry],
         out_shape=(height, width),
         transform=transform,
         fill=0,  # background value
         dtype=dtype
     )
```

1. Return raster array

- **Example**: Protected areas → pixels inside polygons = 1, outside = 0

**`rasterize_continuous(gdf, template, value_column)`**

- **What it does**: Burns **attribute values** into raster (not just presence/absence)
- **Logic flow**: Same as `rasterize_vector`, but shapes include the attribute:

```python
  shapes = [(geom, value) for geom, value in zip(gdf.geometry, gdf[value_column])]
```

- **Example**: Population density → each polygon burns its actual density value (people/km²)

**`reproject_raster(src_path, target_crs, resolution, resampling)`**

- **What it does**: Reprojects an existing raster to match project grid
- **Logic flow**:
    1. Open source raster with `rasterio.open(src_path)`
    2. Calculate destination transform for target CRS + resolution
    3. Use `rasterio.warp.reproject()` with specified resampling method (nearest for categorical, bilinear for continuous)
    4. Return reprojected array + profile
- **Example**: UK Land Cover Map arrives in OSGB36 → reproject to EPSG:27700 at 100m resolution

#### Output Writing

**`write_geotiff(array, profile, path, band_names=None)`**

- **What it does**: Saves raster with consistent metadata
- **Logic flow**:
    1. Update profile with standard settings:

```python
     profile.update({
         'driver': 'GTiff',
         'compress': 'lzw',
         'nodata': -9999,
         'tiled': True,
         'blockxsize': 256,
         'blockysize': 256
     })
```

2. Write array: `rasterio.open(path, 'w', **profile).write(array)`
3. If `band_names` provided, add descriptions to each band
4. Return None

- **Why**: Ensures all outputs have consistent compression, nodata handling, tile structure

#### Zonal Statistics

**`load_zone_shapes(path, target_crs)`**

- **Logic flow**:
    1. Load zones with `geopandas.read_file(path)`
    2. Reproject to target CRS
    3. Validate: check for required columns (e.g., 'zone' or 'name')
    4. Return zone GeoDataFrame

**`calculate_zone_fraction(raster, zones, band=None)`**
- [X] #todo #tocode for line 650 land_utils.py, confirm that zonal.geojson & ssep_zones.geojson uses one of those options for zone name 📅 2026-02-24

- **What it does**: What fraction of each zone is covered by a binary mask?
- **Logic flow**:
    1. For each zone polygon:
        - Mask raster to zone extent: `raster[zone_mask]`
        - Count pixels == 1 (excluded area)
        - Count total pixels in zone
        - Fraction = excluded_pixels / total_pixels
    2. Return DataFrame: `zone | fraction_excluded`
- **Example**: What % of each ESO zone is Natura2000 protected?

**`calculate_zone_summary(raster, zones, stats)`**

- **What it does**: Compute mean/max/percentile of continuous raster per zone
- **Logic flow**:
    1. For each zone:
        - Extract raster values within zone bounds
        - Compute requested statistics (mean, max, P95, etc.)
    2. Return DataFrame: `zone | mean | max | p95 | ...`
- **Example**: Mean population density per ESO zone

#### Validation

**`validate_crs(data, expected_crs)`**

- Assert `data.crs == expected_crs`, raise error if mismatch

**`validate_gb_coverage(data, bounds)`**

- Check data extent overlaps GB bounding box, warn if coverage <90%

---
# NEXT
## Phase 3: Foundation Scripts

> **Goal**: Implement each Layer 1 script. One script per task, in dependency order.
> **Finish before**: Phase 4.
> **Note**: Every script must follow `SCRIPT_PATTERNS.md` template: docstring header, import organization, `sys.path` setup, `setup_logging()`, `snk = globals().get('snakemake')` with fallback, `main()` function, `if __name__ == "__main__":` guard, stage timing, and input validation.
> **All foundation scripts import from `scripts/utilities/land_utils.py`** (written in Phase 2.10) rather than duplicating vector, raster, or zonal operations.

### 3.1 Write `scripts/land/build_protected_areas_raster.py` - DONE

- **Time**: 2hr
- **Action**: Implement the script per workflow Section 4.1 `build_protected_areas_raster`. Read all 24 protected area / environmental designation vector files, reproject to EPSG:27700, merge by tier, dissolve overlapping geometries within each tier, rasterize to 4-band GeoTIFF at configured resolution, calculate zone-level fractions. Use `rasterio` for rasterization. Add input validation (check files exist, geometries are polygons).
- **File(s)**: `scripts/land/build_protected_areas_raster.py`
- **Done when**: Script runs standalone with test data. Produces a 4-band GeoTIFF in EPSG:27700. Produces a CSV with columns: `zone`, `tier1_frac`, `tier2_frac`, `tier3_frac`, `tier4_frac`. All fractions are 0.0-1.0.

#### Tier structure (updated 2026-03-28)

| Band | Tier | Datasets | Files |
|------|------|----------|-------|
| 1 | SAC/SPA/Ramsar/SSSI (excl. marine) | SAC, SPA, Ramsar, SSSI Eng/Sco/Wal — all clipped to GB land boundary | 6 `*_excluding_marine.gpkg` files |
| 2 | AONB/NatParks/NSA | AONB Eng/Wal, NSA Sco, NatParks Eng/Wal/Sco | 6 files |
| 3 | Irreplaceable Habitats | Ancient Woodland Eng/Sco/Wal, irreplaceable habitats Eng/Sco, Wales blanket bog/dunes/limestone/fens | 9 files |
| 4 | Historic Environment | Historic environment Eng/Sco/Wal (WHS, SAM, RPG, battlefields) | 3 files |

**Marine exclusion note:** Tier 1 uses land-only versions of SAC/SPA/Ramsar/SSSI files, clipped to the GB land boundary derived from the land cover raster. In the original data, 72% of SAC/SPA pixels were over sea, inflating zone statistics for coastal zones.

**Config:** Each tier is enabled (hard exclusion) or disabled (skipped) per technology via `protected_tiers` config. No buffer distances — only the polygon footprint is excluded.

**Data flow summary**: 24 vector files → 4 merged GeoDataFrames → 4 raster bands → 1 multi-band GeoTIFF + 1 CSV

### 3.2 Write `scripts/land/build_land_cover_raster.py` - DONE

- **Time**: 1hr
- **Action**: Implement per workflow Section 4.1 `build_land_cover_raster`. Read UK LCM 2024 GeoTIFF, reproject from OSGB36 to EPSG:27700 (likely already in 27700), resample to configured resolution, retain original class codes.
- **File(s)**: `scripts/land/build_land_cover_raster.py` (new)
- **Done when**: Script produces a single-band GeoTIFF in EPSG:27700 at configured resolution. Pixel values are integer land cover class codes. File opens with rasterio without error.

#### code logic

**Purpose**: Reproject UK Land Cover Map from OSGB36 to project CRS, resample to 100m, retain original class codes

**Stage 1: Load source raster**

```python
with rasterio.open(input.lcm) as src:
    src_crs = src.crs  # Typically EPSG:27700 (OSGB36)
    src_data = src.read(1)  # Single band
    src_profile = src.profile
```

**Stage 2: Define target grid**

```python
# Use canonical GB bounds — all foundation rasters share the same grid
template = create_reference_grid()  # defaults: canonical GB bounds, 100m, EPSG:27700
```

**Stage 3: Reproject raster**

```python
dst_array, dst_transform = reproject_raster(
    input.lcm,
    target_crs=27700,
    resolution=100,
    resampling='nearest'  # Preserve categorical values
)
```

**Stage 4: Validate class codes**

```python
unique_classes = np.unique(dst_array)
expected_classes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]  # LCM 2024 codes
assert set(unique_classes).issubset(expected_classes), "Unexpected land cover classes"
```

**Stage 5: Write output**

```python
profile = {
    'driver': 'GTiff',
    'count': 1,
    'crs': 27700,
    'transform': dst_transform,
    'width': dst_array.shape[1],
    'height': dst_array.shape[0],
    'dtype': 'uint8',
    'nodata': 0
}
write_geotiff(dst_array, profile, output.raster)
```

**Data flow**: Input GeoTIFF (OSGB36, varying resolution) → Reprojected GeoTIFF (EPSG:27700, 100m, aligned to reference grid)

### 3.3 Write `scripts/land/build_population_density_surface.py` - DONE, NOT VALIDATED

- **Time**: 2hr
- **Action**: Implement per workflow Section 4.1. Read Census 2021 OA data: Scotland GPKG has population counts embedded (`Popcount`, `HHcount`, `sqkm` columns, 46,363 OAs); England & Wales GPKG contains boundaries only (`OA21CD` code, 188,880 OAs) — population density (people/km²) is joined from a separate Census 2021 TS006 CSV at OA level via `OA21CD` → `Output Areas Code`. Calculate population density per OA, rasterize to continuous density surface (people/km²) at configured resolution in EPSG:27700. Zone-level population statistics are computed by `calculate_zone_statistics` (not this script).
- **Input data**:
  - `data/land/societal/output_areas_ew.gpkg` — E&W OA boundaries (188,880 features, EPSG:27700). Columns: `OA21CD`, `LSOA21CD`, `LSOA21NM`, `LSOA21NMW`, `BNG_E`, `BNG_N`, `GlobalID`. No population columns.
  - `data/land/societal/output_areas_ew.csv` — Census 2021 TS006 population density at OA level (188,880 rows). Columns: `Output Areas Code`, `Output Areas`, `Observation` (density in people/km²). Source: ONS NOMIS TS006 filtered to OA geography.
  - `data/land/societal/output_areas_scotland.gpkg` — Scotland OA boundaries with population (46,363 features, EPSG:27700). Columns: `code`, `HHcount` (int), `Popcount` (int), `sqkm` (float), plus `council`, `masterpc`, `easting`, `northing`.
- **File(s)**: `scripts/land/build_population_density_surface.py` (new)
- **Done when**: Produces a single-band GeoTIFF with continuous density values. No zone CSV output. Consumed by `build_nuclear_pop_criterion`. Density values are plausible (London areas > 5000, rural Scotland < 50).

#### code logic

**Purpose**: Convert Census 2021 Output Area (OA) data into **continuous density raster**

Consumed by `build_nuclear_pop_criterion` (ONR demographic siting criterion),
which reads this raster as one of its inputs. No separate zone CSV is produced here.

**Stage 1: Load OA boundaries and join population density**

```python
# Scotland — population counts embedded in GPKG (Popcount, sqkm columns)
oa_sco = load_and_reproject_vector(input.oa_shapes_sco, target_crs=27700)
# Calculate density from embedded columns
oa_sco['density'] = oa_sco['Popcount'] / oa_sco['sqkm']  # people/km²

# England/Wales — boundaries only (no population column in GPKG)
oa_ew = load_and_reproject_vector(input.oa_shapes_ew, target_crs=27700)

# Join density from Census 2021 TS006 CSV via OA code
# CSV columns: Output Areas Code, Output Areas, Observation (density people/km²)
density_csv = pd.read_csv(input.oa_density_ew)
density_csv = density_csv.rename(columns={
    'Output Areas Code': 'OA21CD',
    'Observation': 'density'
})
oa_ew = oa_ew.merge(density_csv[['OA21CD', 'density']], on='OA21CD', how='left')

# Validate join — all OAs should have density values
assert oa_ew['density'].notna().all(), (
    f"Join failed: {oa_ew['density'].isna().sum()} E&W OAs missing density"
)
```

**Stage 2: Combine GB-wide**

```python
# Standardise columns and combine
oa_sco_std = oa_sco[['geometry', 'density']].copy()
oa_ew_std = oa_ew[['geometry', 'density']].copy()
oa_gb = pd.concat([oa_ew_std, oa_sco_std], ignore_index=True)
```

**Stage 3: Create reference grid**

```python
# Use canonical GB bounds — all foundation rasters share the same grid
template = create_reference_grid()  # defaults: canonical GB bounds, 100m, EPSG:27700
```

**Stage 4: Rasterize density**

```python
density_raster = rasterize_continuous(
    oa_gb,
    template,
    value_column='density'
)
```

**Stage 5: Write output**

```python
write_geotiff(density_raster, profile, output.raster)
```

**Data flow**: E&W GPKG (boundaries) + E&W CSV (density) + Scotland GPKG (boundaries + Popcount) → joined GeoDataFrame with density column → Continuous raster

### 3.4 Write `scripts/land/build_flooding_risk_raster.py` - DONE

- **Time**: 1hr (initial), +2hr (memory optimisation)
- **Action**: Merge EA (England), SEPA (Scotland — river, surface water, coastal), and NRW (Wales — rivers & seas, surface water) flood zones. 6 input files totalling ~13 GB. Uses chunked fiona streaming to keep memory bounded (~500 MB peak instead of ~20 GB). Rasterize to binary raster at 100m resolution in EPSG:27700.
- **File(s)**: `scripts/land/build_flooding_risk_raster.py`
- **Done when**: Produces a single-band uint8 GeoTIFF in EPSG:27700. Non-zero pixels indicate flood risk areas. 13 tests pass.

#### code logic

**Purpose**: Merge all EA/SEPA/NRW flooding data (river, surface water, coastal) into unified binary risk raster

**Memory-efficient approach**: The 6 input GPKGs total ~13 GB on disk, which would exceed WSL memory limits if loaded simultaneously. Instead of load-all → dissolve → rasterize, the script uses **chunked fiona streaming**: each file is read in batches of 10,000 features via `fiona.open()`, reprojected with `pyproj.Transformer`, rasterized with `rasterio.features.rasterize`, and OR-merged into an accumulator array. The `dissolve_overlaps` step is eliminated entirely — since the output is binary (1 = flood, 0 = no flood), overlapping polygons are handled idempotently by `np.maximum`.

**Stage 1: Create canonical reference grid**

```python
width, height, transform, crs = create_reference_grid(resolution=100, crs="EPSG:27700")
flood_raster = np.zeros((height, width), dtype="uint8")  # accumulator
```

**Stage 2: For each input file, stream and rasterize in chunks**

```python
for name, path in input_paths.items():
    with fiona.open(path) as src:
        chunk = []
        for feat in src:
            geom = shape(feat["geometry"])
            if needs_reproject:
                geom = transform(reproject_fn, geom)
            chunk.append(geom)

            if len(chunk) >= CHUNK_SIZE:  # 10,000 features
                burned = features.rasterize([(g, 1) for g in chunk], ...)
                np.maximum(flood_raster, burned, out=flood_raster)
                del chunk; gc.collect()
                chunk = []
        # Process remaining features in final partial chunk
```

**Stage 3: Write output**

```python
write_geotiff(flood_raster, profile, output_raster)
```

**Data flow**: 6 GPKG files (EA + 3 SEPA + 2 NRW, ~13 GB) → streamed in 10k-feature chunks → Binary raster (1 = flood risk, 0 = none)
**Peak memory**: ~500 MB (chunk geometries + 2 raster arrays) vs ~20 GB (previous load-all approach)

### 3.5 Write `scripts/land/build_groundwater_protection.py` - DONE, NOT VALIDATED

- **Time**: 1hr
- **Action**: Create a **3-band** raster from Source Protection Zone (SPZ) data for England and Wales only. Scotland does not implement similar protection areas around drinking water sources and is excluded. Band 1 = SPZ1 (inner protection zones), Band 2 = SPZ2 (outer protection zones), Band 3 = SPZ3 (total catchment zones). Zone 4 (special interest) is dropped — no formal protection, not referenced by any config parameter.
- **Input**: 2 files — `source_protection_zones_england.gpkg` (Defra/EA), `source_protection_zones_wales.gpkg` (NRW)
- **File(s)**: `scripts/land/build_groundwater_protection.py`
- **Done when**: Produces a 3-band uint8 GeoTIFF (`groundwater_spz_ew.tif`) in EPSG:27700. Band 1 = SPZ1, Band 2 = SPZ2, Band 3 = SPZ3.

#### code logic

**Purpose**: Create 3-band SPZ raster for England & Wales (Scotland excluded)

**Stage 1: Load E&W SPZ data**

```python
spz_eng = load_and_reproject_vector(input.spz_eng, target_crs=27700)
spz_wal = load_and_reproject_vector(input.spz_wal, target_crs=27700)
all_spz = pd.concat([spz_eng, spz_wal])
```

**Stage 2: Classify into SPZ1, SPZ2, and SPZ3**

```python
# Cast number column to string (handles dtype mismatch: England=object, Wales=int16)
zone_str = all_spz["number"].astype(str)
spz1 = all_spz[zone_str.str.startswith("1")]    # captures '1' and '1c'
spz2 = all_spz[zone_str.str.startswith("2")]    # captures '2' and '2c'
spz3 = all_spz[zone_str.str.startswith("3")]    # captures '3' and '3c'
# Zone 4 (special interest) is dropped
```

**Stage 3: Dissolve each subset independently, rasterize to separate bands**

```python
band1 = rasterize_vector(dissolve_overlaps(spz1), template, burn_value=1)
band2 = rasterize_vector(dissolve_overlaps(spz2), template, burn_value=1)
band3 = rasterize_vector(dissolve_overlaps(spz3), template, burn_value=1)
spz_raster = np.stack([band1, band2, band3], axis=0)  # shape: (3, height, width)
```

**Config mapping**: `exclusion_zones: 1` → Band 1 only; `exclusion_zones: [1, 2]` → Bands 1+2; `exclusion_zones: [1, 2, 3]` → all 3 bands

**Data flow**: 2 vector files (England + Wales) → classified by zone number → 3-band GeoTIFF (SPZ1, SPZ2, SPZ3)

### 3.6 Write `scripts/land/build_green_belt_raster.py` - DONE, NOT VALIDATED

- **Time**: 1hr
- **Action**: Implement per workflow Section 4.1 `build_green_belt_raster`. Merge Green Belt boundaries from England (and Scotland if available). Rasterize to binary mask at configured resolution in EPSG:27700. Green Belt is a **soft constraint** (penalty, not hard exclusion) for onwind, solar, nuclear, and hydrogen technologies.
- **File(s)**: `scripts/land/build_green_belt_raster.py` (new)
- **Done when**: Produces a single-band GeoTIFF in EPSG:27700. Pixels = 1 for Green Belt, 0 elsewhere. File opens with rasterio without error.

### 3.7 Write `scripts/land/build_alc_raster.py` - DONE, NOT VALIDATED

- **Time**: 1hr
- **Action**: Implement per workflow Section 4.1 `build_alc_raster`. Merge Agricultural Land Classification data from England (provisional), Wales (predictive), and Scotland. Classify into Best and Most Versatile (BMV) agricultural land (Grades 1, 2, 3a) vs non-BMV. Rasterize to binary mask at configured resolution in EPSG:27700.
- **File(s)**: `scripts/land/build_alc_raster.py` (new)
- **Done when**: Produces a single-band GeoTIFF in EPSG:27700. Pixels = 1 for BMV land, 0 elsewhere. File opens with rasterio without error.

### 3.8 Write `scripts/land/build_airfield_raster.py` - DONE, VALIDATED

- **Time**: 1.5hr (initial), 1hr (FRZ refactor)
- **Action**: Build 2-band FRZ exclusion raster from authoritative UK AIP ENR 5.1 data. Reads pre-extracted civil and MoD aerodrome FRZ CSVs (`data/land/hazards/civil_aerodromes_frz.csv`, `data/land/hazards/mod_aerodromes_frz.csv`) containing centre coordinates (WGS84) and FRZ circle radii (km). Buffers each centre point by its FRZ radius and rasterizes to a 2-band GeoTIFF.
  - **Band 1 — MoD/Military FRZ circles**: 42 aerodromes classified via keyword dictionary (RAF, RNAS, AAC, USAF, QinetiQ bases)
  - **Band 2 — Civilian FRZ circles**: 116 aerodromes (all others)
- **Data provenance**: FRZ data extracted from `data/land/hazards/raw/airfields_protected_military.xlsx` (UK AIP ENR 5.1, sheet "restricted") by `scripts/land/extract_aerodrome_frz.py`. Only EGRnU 'A' suffix entries (primary ATZ circles) retained.
- **Purpose**: The FRZ radius IS the exclusion zone — no downstream `airport_buffer` config needed. All onshore technologies (onwind, solar) apply this raster as hard exclusion.
- **File(s)**: `scripts/land/build_airfield_raster.py`
- **Done when**: Produces a 2-band GeoTIFF in EPSG:27700 at 100m resolution. Each band is binary (1 = FRZ circle, 0 = absent). 158 aerodromes total (116 civil + 42 MoD). 25 unit tests pass.

### 3.8b Write `scripts/land/build_coastal_erosion_raster.py` - DONE, VALIDATED

- **Time**: 1hr
- **Action**: Build single-band coastal erosion exclusion raster from NCERM 2024 data. Loads 3 named layers from the multi-layer GeoPackage (`NCERM_SMP_2105_70CC`, `NCERM_Ground_Instability_Zone`, `NCERM_Ground_Instability_Recession`), rasterizes each with `rasterize_vector()` from `land_utils`, and OR-merges into a single binary accumulator. Data is already EPSG:27700 (no reprojection needed). Uses geopandas bulk loading (not fiona chunked streaming) since data is small (~22k features across 3 layers).
  - **EN-7 rationale**: EN-7 (Nuclear NPS) requires consideration of coastal change for energy infrastructure siting. The 2105 time horizon with 70th percentile climate change was chosen to reflect ~60-year operational lifetime of nuclear assets plus decommissioning.
  - **Layer selection**: SMP (Shoreline Management Plans) was chosen over NFI (No Further Intervention) as SMP reflects the planned policy-delivered scenario (coastal defences maintained). Ground instability layers capture both historical and predicted cliff recession/landslide zones.
- **Data provenance**: National Coastal Erosion Risk Mapping (NCERM) - National (2024), Environment Agency. Downloaded from https://environment.data.gov.uk/dataset/9fede91f-5acd-4fd2-9bd8-98153fa3c2ff
- **Purpose**: Foundation raster consumed by `build_onshore_renewable_exclusions` (hard exclusion for onwind/solar, buffered exclusion for nuclear SMR). Also consumed by downstream nuclear and hydrogen exclusion zone builders.
- **Config**: `land_constraints.coastal_change.enabled`, `land_constraints.coastal_change.layers` (list of 3 GPKG layer names). Per-technology: `onwind.coastal_change.enabled`, `solar.coastal_change.enabled`, `nuclear.siting_constraints.smr.coastal_change.enabled` + `buffer_m: 1000`, `hydrogen.siting_constraints.electrolysis.coastal_change.enabled`, `hydrogen.siting_constraints.h2_turbine.coastal_change.enabled`.
- **File(s)**: `scripts/land/build_coastal_erosion_raster.py`
- **Done when**: Produces a single-band GeoTIFF in EPSG:27700 at 100m resolution. Binary mask: 1 = erosion/instability risk, 0 = safe. 27 unit tests pass.

### 3.9 ~~Write `scripts/land/calculate_zone_statistics.py`~~ — DONE then REMOVED (2026-03-29)

> **Removed from DAG**: Rule `calculate_zone_statistics` was removed because each per-technology exclusion rule now does pixel-level intersection and includes `area_km2` in its availability CSV. The per-layer fraction breakdown is no longer needed for the pipeline. Script and tests retained in codebase for potential diagnostic use.

### ~~3.9 original~~ Write `scripts/land/calculate_zone_statistics.py` - DONE

- **Time**: 2hr
- **Action**: Read all foundation rasters (protected areas 4-band, airfields 2-band FRZ, land cover, flooding risk, groundwater SPZ 3-band) + zone shapes. For each zone, compute zonal statistics. Uses `{network_model}` wildcard — multiple scenarios sharing the same network model reuse one CSV.
- **File(s)**: `scripts/land/calculate_zone_statistics.py`
- **Done when**: Produces a CSV with one row per zone and 17 data columns. All fractions are 0.0-1.0. No NaN values in required columns. Zone count matches the zone shapes file. 31 tests pass.

**Output CSV columns** (18 data columns + zone index):
`zone`, `area_km2`, `protected_tier1_frac`, `protected_tier2_frac`, `protected_tier3_frac`, `protected_tier4_frac`, `airfield_mod_frac`, `airfield_civilian_frac`, `flood_risk_frac`, `spz1_frac`, `spz2_frac`, `spz3_frac`, `coastal_erosion_frac`, `lc_01_broadleaf_woodland_frac` ... `lc_21_suburban_frac` (21 land cover class fractions), `coastal` (bool)

**Note**: Airfield fractions now represent FRZ circle coverage from the 2-band raster (MoD FRZ, Civilian FRZ). Land cover fractions are computed per LCM 2024 class (codes 1–21); downstream technology configs define which codes to exclude. Coastal erosion fraction represents the proportion of each zone covered by NCERM 2024 erosion/instability polygons (3 layers OR-merged).

### 3.10 Write `scripts/land/build_scotland_mask.py`

- **Time**: 30min
- **Action**: Implement per workflow Section 4.1. Read zone shapes, identify Scottish zones from config (`scotland.zones`), output a binary raster mask and a CSV listing Scottish zone names.
- **File(s)**: `scripts/land/build_scotland_mask.py` (new)
- **Done when**: Raster has value 1 for Scotland pixels, 0 elsewhere. CSV lists the Scottish zone identifiers. Zone names match config.

contributing

## Code Review

All contributions go through code review:

1. **Functionality**: Does it work as intended?
2. **Tests**: Are there adequate tests?
3. **Documentation**: Is it documented?
4. **Style**: Does it follow guidelines?
5. **Performance**: Any performance concerns?

conduct a code review on @scripts/utilities/land_utils.py & @tests/unit/test_land_utils.py to confirm: 1. @scripts/utilities/land_utils.py follows @docs/templates/SCRIPT_PATTERNS.md 2. @tests/unit/test_land_utils.py follows @docs/templates/TESTING_PATTERNS.md 3. Does it work as intended to be used in @docs/planning/workflow_plans/unified_land_constraints_workflow.md and for scripts outlined in @docs/planning/land_constraints_tasklist.md, relevant Phases 3 Foundation, 6 Nuclear, 7 Hydrogen, 8 Renewables 4. Are there adequate tests? 5. Does it follow style guidelines @scripts/README.md @tests/README.md? 6. Any performance concerns? 7. complies with @docs/source/development/contributing.md

---

## Phase 4: Foundation Snakemake Rules

> **Goal**: Create `rules/land_constraints.smk` and wire each foundation script into a Snakemake rule (Section A).
> **Finish before**: Phase 5.
> **Architecture**: All land constraint rules live in a **single file** `rules/land_constraints.smk` with three sections: A (foundation), B (renewables), C (nuclear & hydrogen). This phase creates the file and populates Section A only. Sections B and C are added in Phases 6-8. Only **one** `include:` statement is needed in the Snakefile.
> **Note**: Every rule must comply with `rules/STYLE_GUIDE.md`: docstring, named inputs, `wildcard_constraints`, `message`, `log`, `benchmark`, `conda`, `script`. Use `f"{resources_path}/..."` paths. Do NOT redefine `resources_path`.

### 4.1 Create `rules/land_constraints.smk` with module docstring, section headers, and helper functions - DONE

- **Time**: 30min
- **Action**: Create the **single** rule file that will hold ALL land constraint rules (foundation, renewables, nuclear, hydrogen). Start with: (1) module docstring per STYLE_GUIDE Section 1, (2) section header comments for Section A, B, C (B and C initially empty — populated in Phases 6-8), (3) helper function `_get_zone_shapes(wildcards)` that returns the zone shapes path for the scenario's network model, (4) helper `_get_network_model(wildcards)` that returns `scenarios[wildcards.scenario].get('network_model', 'ETYS')`. See workflow doc Section 3 for the architecture overview.
- **File(s)**: `rules/land_constraints.smk` (new)
- **Done when**: File has docstring, three section header comments (`# SECTION A: SHARED FOUNDATION`, `# SECTION B: RENEWABLE CONSTRAINTS`, `# SECTION C: NUCLEAR & HYDROGEN CONSTRAINTS`), helper functions, no rules yet. Python syntax is valid.

### 4.2 Add `build_protected_areas_raster` rule - DONE

- **Time**: 30min
- **Action**: Add rule to `rules/land_constraints.smk`. No wildcards (GB-wide output). Input: 24 vector files from `data/land/environment/` (6 Tier 1 excl. marine, 6 Tier 2 AONB/NatParks/NSA, 9 Tier 3 irreplaceable habitats, 3 Tier 4 historic environment). Output: `resources/land/protected_areas_gb.tif` (4-band), `resources/land/protected_area_fractions.csv`. Include `resources: mem_mb=8000`.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Rule has all required directives per STYLE_GUIDE. `snakemake -n resources/land/protected_areas_gb.tif` shows the planned execution (dry-run).

### 4.3 Add `build_land_cover_raster` rule - DONE

- **Time**: 15min
- **Action**: Add rule. No wildcards. Input: `data/land/environment/uk_lcm_2024.tif`. Output: `resources/land/land_cover_gb.tif`.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.4 Add `build_population_density_surface` rule - DONE

- **Time**: 30min
- **Action**: Add rule. No wildcard — raster output is zone-independent (like other foundation rasters). Input: OA boundary GPKGs. Output: `population_density_gb.tif` only. Consumed by `build_nuclear_pop_criterion`. No separate zone CSV from this rule.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds. Rule has no wildcard (same as `build_flooding_risk_raster` etc.).

### 4.5 Add `build_flooding_risk_raster` rule - DONE

- **Time**: 15min
- **Action**: Add rule. No wildcards. Input: 6 flooding data files from `data/land/hazards/` (EA, 3x SEPA, 2x NRW). Output: `resources/land/flooding_risk_gb.tif`.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.6 Add `build_groundwater_protection` rule - DONE

- **Time**: 15min
- **Action**: Add rule. No wildcards. Input: 2 SPZ files from `data/land/environment/` (Defra/EA England, NRW Wales). Output: `resources/land/groundwater_spz_ew.tif` (3-band: SPZ1, SPZ2, SPZ3). Scotland excluded (no equivalent SPZ system).
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.7 Add `build_green_belt_raster` rule - DONE

- **Time**: 15min
- **Action**: Add rule. No wildcards. Input: `green_belt_england.gpkg`, `green_belt_scotland.gpkg` from `data/land/societal/`. Output: `resources/land/green_belt_gb.tif`.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.8 Add `build_alc_raster` rule - DONE

- **Time**: 15min
- **Action**: Add rule. No wildcards. Input: 3 pre-filtered BMV GeoPackages from `data/land/societal/` (`alc_bmv_england.gpkg`, `alc_bmv_wales.gpkg`, `alc_bmv_scotland.gpkg`). Output: `resources/land/alc_bmv_gb.tif`. Hard constraint for solar and nuclear SMR.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.9 Add `build_airfield_raster` rule - DONE

- **Time**: 15min
- **Action**: Add rule. No wildcards. Input: `data/land/hazards/civil_aerodromes_frz.csv` and `data/land/hazards/mod_aerodromes_frz.csv`. Output: `resources/land/airfields_gb.tif` (2-band: MoD FRZ circles, Civilian FRZ circles). Params: `resolution` and `target_crs` from foundation config. No `mod_patterns` or `international_airports` params — classification is pre-done in the FRZ CSVs.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.9b Add `build_coastal_erosion_raster` rule - DONE

- **Time**: 15min
- **Action**: Add rule to Section A of `rules/land_constraints.smk`, after `build_airfield_raster` and before `calculate_zone_statistics`. No wildcards (GB-wide foundation raster). Input: `data/land/hazards/coastal_erosion_uk_2024.gpkg`. Output: `resources/land/coastal_erosion_gb.tif` (single-band binary). Params: `layers` from `coastal_change` config, `resolution` and `target_crs` from foundation config. Also wired `coastal_erosion` input to downstream rules `calculate_zone_statistics` and `build_onshore_renewable_availability` to establish DAG dependency.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Rule added. Downstream rules have `coastal_erosion` input. DAG resolves correctly.

### 4.10 ~~Add `calculate_zone_statistics` rule~~ — DONE then REMOVED (2026-03-29)

- **Time**: 30min
- **Action**: Add rule. Uses `{network_model}` wildcard. Input: 9 foundation rasters (protected_areas, airfields, land_cover, flooding_risk, groundwater_spz, coastal_erosion, alc_bmv, green_belt) + zone shapes via `get_zones_for_model`. Output: `resources/land/zone_statistics_{network_model}.csv`.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds.

### 4.11 Add `build_scotland_mask` rule

- **Time**: 15min
- **Action**: Add rule. Uses `{network_model}` wildcard (same deduplication rationale as `calculate_zone_statistics`). Input: zone shapes via `get_zones_for_model`. Output: `scotland_mask_{network_model}.tif` + `scotland_zones_{network_model}.csv`. Params: `scottish_zones` from config. Add `wildcard_constraints: network_model="[^/]+"`.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: Dry-run succeeds. Multiple scenarios with same network_model produce only one mask/CSV pair.

### 4.12 Add `include: "rules/land_constraints.smk"` to Snakefile - DONE

- **Time**: 15min
- **Action**: Add the include statement to the main `Snakefile`, following the pattern of existing includes. Place it after the existing rule file includes. This is the **only** include needed for the entire land constraints system — all foundation, renewable, nuclear, and hydrogen GIS rules live in this one file.
- **File(s)**: `Snakefile`
- **Done when**: `snakemake -n --list` shows all new rules from `land_constraints.smk`. No additional include statements will be needed for land constraints in later phases.

---

## Phase 5: Foundation Validation

> **Goal**: Run the foundation pipeline and verify outputs.
> **Finish before**: Phase 6.

### 5.1 Write unit tests for `build_protected_areas_raster.py` - DONE

- **Time**: 1hr
- **Action**: Create `tests/unit/test_build_protected_areas_raster.py`. Test: input validation (missing files raise error), tier merging logic (overlapping geometries dissolve correctly), output raster has 4 bands (including ancient woodland), zone fractions are 0.0-1.0. Use synthetic test geometries (small rectangles), not real data. Follow TESTING_PATTERNS.md template.
- **File(s)**: `tests/unit/test_build_protected_areas_raster.py` (new)
- **Done when**: `pytest tests/unit/test_build_protected_areas_raster.py -v` passes. At least 5 test cases.

### 5.1b Write unit tests for `build_coastal_erosion_raster.py` - DONE

- **Time**: 45min
- **Action**: Create `tests/unit/test_build_coastal_erosion_raster.py`. Tests: constants validation (3 default layers, correct names, band name), input validation (missing file raises FileNotFoundError), layer loading from multi-layer GPKG (correct count, CRS is 27700, all 3 layers loadable, geometry type is MultiPolygon), component-level rasterization (2D output, uint8 dtype, binary values, has erosion pixels, OR-merge captures all layers, overlapping layers idempotent), build function integration (returns tuple, 2D, uint8, binary, has pixels, profile single-band/CRS/dtype), GeoTIFF round-trip (file exists, rasterio readable, data matches original, band description set). Uses synthetic 3-layer GPKG fixture with MultiPolygon geometries in EPSG:27700, small_bounds (10km × 10km) for fast execution. Follows `test_build_airfield_raster.py` pattern.
- **File(s)**: `tests/unit/test_build_coastal_erosion_raster.py` (new)
- **Done when**: `pytest tests/unit/test_build_coastal_erosion_raster.py -v` passes. 27 test cases across 7 test classes.

### 5.2 ~~Write unit tests for `calculate_zone_statistics.py`~~ — DONE (retained, rule removed from DAG)

- **Time**: 1hr
- **Action**: Create `tests/unit/test_calculate_zone_statistics.py`. Test: output has expected columns, fractions are 0-1, coastal detection works, handles zones with no raster coverage. Use synthetic rasters and zones.
- **File(s)**: `tests/unit/test_calculate_zone_statistics.py` (new)
- **Done when**: `pytest tests/unit/test_calculate_zone_statistics.py -v` passes.

### 5.3 Run foundation pipeline with real data (manual test)

- **Time**: 1hr
- **Action**: With real data in place, run `snakemake resources/land/zone_statistics_{network_model}.csv --cores 4` for a test network model (e.g., ETYS). Visually inspect the protected areas raster (plot in QGIS or matplotlib — do protected areas align with known locations?). Check zone statistics CSV for plausibility.
- **File(s)**: No files modified — manual validation
- **Done when**: Zone statistics CSV exists, has correct number of zones, fractions are plausible. Protected areas raster visually correct. No errors in logs.

---

## Phase 6: Nuclear Branch

> **Goal**: Implement nuclear siting constraints (simpler than renewables — do first to validate the foundation→branch interface).
> **Finish before**: Phase 7.

### 6.1 Write `scripts/land/nuclear_pop_density_criterion.py` — **COMPLETE**

- **Time**: 8hr (implementation + compliance review + optimisation)
- **Action**: Implement the ONR Semi-Urban Demographic Criterion as a Snakemake script. This is the formal regulatory test that determines whether a 100m grid square is demographically suitable for nuclear siting (SPF_MAX < 1). The criterion has two phases:
  - **Phase 1 (all-around):** For each pixel, compute cumulative weighted population (CWP) summed over all directions using 30 annular band FFT convolutions (1–30 km). Check SPF_360(r) = CWP_360(r) / CWP_bar_360(r) < 1 at every cumulative radius r = 2..30 km. The hypothetical reference is 1000 persons/km² with zero population within 1 km. Weighting follows ONR Equation (2): rm = sqrt((r² + (r-1)²) / 2), and Equation (1): Wr = rm^{-1.5}. Band 1 (0–1 km) is included in the actual CWP but not the hypothetical threshold.
  - **Phase 2 (sector):** For candidate pixels that passed the all-around test but have CWP_360(r) > T_30(r) at some radius (since sector CWP ≤ all-around CWP), decompose population into 72 five-degree angular slices across 30 radial bands. At each cumulative radius, compute 30° sector CWPs via rolling sum of 6 consecutive slices. Check SPF_theta(r) = CWP_theta(r) / CWP_bar_30(r) < 1 for all 72 sectors and all radii. The hypothetical sector reference is 5000 persons/km² over 30°/360° = 1/12 of the circle.
  - **Scotland exclusion:** Population density is zeroed in Scotland *before* the FFT convolution so that Scottish population does not inflate English/Welsh border pixel CWPs within the 30 km kernel radius. Scottish pixels are separately flagged as ineligible (nuclear ban). The Scotland mask is an input from the existing `build_scotland_mask` rule.
  - **Performance:** Phase 1 uses `scipy.signal.fftconvolve` (~2 min for 30 bands). Phase 2 uses `concurrent.futures.ProcessPoolExecutor` with `multiprocessing.shared_memory.SharedMemory` to process candidate chunks in parallel (~25–30 min with 4 workers). Candidates are spatially sorted for cache-friendly memory access.
  - **Outputs:** Single-band uint8 GeoTIFF (0 = eligible/SPF_MAX < 1, 1 = ineligible, 255 = nodata) and per-zone CSV with `pop_criterion_eligible_frac` for downstream `build_nuclear_eligibility`.
- **File(s)**: `scripts/land/nuclear_pop_density_criterion.py` (rewritten from stub), `rules/land_constraints.smk` (rule added to Section C), `tests/unit/test_nuclear_pop_density_criterion.py` (36 tests)
- **Done when**: Script runs standalone. Output raster has correct shape/CRS/dtype. All-around ineligible ~3.1–3.2M E&W pixels. Sector additions ~60–80K. Zone fractions CSV has all onshore zones with values 0–1. Scottish zones have `eligible_frac = 0`. 31 unit tests pass in <5s; 5 slow pipeline tests pass with synthetic data.

### 6.2 ~~Write `scripts/generators/build_nuclear_exclusion_zones.py`~~ DONE (2026-03-29)

- **Time**: 2hr (actual: ~1hr)
- **Action**: Implemented per workflow Section 4.3, simplified scope. Reads COMAH Upper Tier sites (CSV with easting/northing → Point GeoDataFrame, 3km buffer) and high-pressure gas pipelines (shapefile, 100m buffer). Pure vector-zone intersection via `union_all()` + `intersects()` per zone. Reservoirs, seismic, fracking, and deep geothermal exclusions removed — only COMAH and gas pipe remain.
- **Config change**: Restructured `nuclear.siting_constraints.smr.gas_pipe_buffer`/`comah_buffer` to nested `gas_pipe.enabled`/`gas_pipe.buffer` and `comah.enabled`/`comah.buffer` pattern for consistency with other SMR constraints.
- **File(s)**: `scripts/land/build_nuclear_exclusion_zones.py` (new), `rules/land_constraints.smk` (rule added to Section C), `tests/unit/test_build_nuclear_exclusion_zones.py` (25 tests), `config/defaults.yaml` (config restructured)
- **Done when**: ~~Script runs standalone. Output CSV has columns: `zone`, `reservoir_buffer_conflict`, `gas_infra_conflict`, `seismic_conflict`, `geothermal_conflict`, `any_nuclear_exclusion`. All values are boolean.~~ **DONE**: 25 unit tests pass in <1s. Output CSV has columns: `zone_name`, `comah_buffer_conflict`, `gas_pipe_buffer_conflict`, `any_nuclear_exclusion`. Uses `{network_model}` wildcard (not `{scenario}`). Output at `resources/land/nuclear_exclusion_zones_{network_model}.csv`.

### 6.3 ~~Write `scripts/generators/build_nuclear_eligibility.py`~~ → REPLACED by `build_nuclear_smr_exclusions` — **COMPLETE** (2026-03-29)

- **Time**: 3hr (actual: ~2hr including plan + implementation + validation)
- **Original plan**: Zone-level boolean eligibility screening from pre-computed CSV fractions.
- **What changed**: Kate identified that independently computed per-layer fractions overlap spatially, making threshold-based screening unreliable. Replaced with pixel-level raster exclusion (same approach as renewables). EN-6 sites removed from scope — large nuclear is handled by `generators.smk`, not this rule.
- **Action**: Created `scripts/land/build_nuclear_smr_exclusions.py` — pixel-level exclusion combining 8 shared foundation layers + 5 nuclear-specific layers (Scotland ban, ONR pop criterion, COMAH 3km buffer, gas pipe 100m buffer, water availability placeholder). Outputs both exclusion raster (.tif) and per-zone availability CSV.
- **Config**: Added `water_constraint.enabled: false` and `capacity_density: 1.0` to `nuclear.siting_constraints.smr` in `defaults.yaml`.
- **File(s)**: `scripts/land/build_nuclear_smr_exclusions.py` (new), `rules/land_constraints.smk` (rule added to Section B), `config/defaults.yaml` (updated), `tests/unit/test_build_nuclear_smr_exclusions.py` (49 tests)
- **Done when**: ~~Eligibility CSV~~ **DONE**: 49 unit tests pass in <1s. Exclusion raster `smr_exclusions_Zonal.tif` (1.3 MB) and availability CSV `smr_availability_Zonal.csv` produced. Scottish zones = 0.0. SMR fracs always <= pop_criterion_eligible_frac (0 violations). 94.7% of GB raster excluded. Highest availability: Z11 (44%), Z12 (44%), Z17 (38%).

### 6.4 ~~Add nuclear rules to `rules/land_constraints.smk`~~ — **COMPLETE** (merged into 6.2 & 6.3)

- `build_nuclear_exclusion_zones` added in 6.2 (diagnostic CSV, retained)
- `build_nuclear_smr_exclusions` added in 6.3 (pixel-level exclusion + availability)
- Both use `{network_model}` wildcard. DAG correct.

### 6.5 ~~Write unit tests for nuclear eligibility logic~~ → **COMPLETE** (merged into 6.3)

- `tests/unit/test_build_nuclear_exclusion_zones.py` — 25 tests (from 6.2)
- `tests/unit/test_build_nuclear_smr_exclusions.py` — 49 tests (from 6.3)
- Total: 74 nuclear unit tests, all passing

### 6.6 Run nuclear branch with real data (manual test) — **COMPLETE** (2026-03-29)

- **Action**: Ran `snakemake resources/land/smr_exclusions_Zonal.tif resources/land/smr_availability_Zonal.csv --cores 4 --forcerun build_nuclear_smr_exclusions --rerun-triggers mtime`. Completed in 46s.
- **Validated**: Scottish zones (Z1_1..Z6) all 0.0. England/Wales zones range 0.007 (Z14) to 0.440 (Z11). SMR fracs <= pop_criterion_eligible_frac (0 violations). Raster ready for QGIS review.

---

## Phase 7: Hydrogen Branch

> **Goal**: Implement hydrogen siting constraints using the **same three-step pipeline** as nuclear SMR and renewables: pixel-level raster exclusion → availability CSV → technical potential.
> **Finish before**: Phase 8.
>
> **2026-03-29 Architecture Update**: The original design used zone-level boolean eligibility (similar to the original nuclear design). This has been replaced with pixel-level raster exclusion to match the unified pipeline architecture. `build_hydrogen_exclusion_zones` and `build_hydrogen_eligibility` are replaced by a single `build_hydrogen_exclusions` rule that follows the same pattern as `build_nuclear_smr_exclusions`.

### 7.1 Write `scripts/land/build_hydrogen_exclusions.py`

- **Time**: 2hr
- **Action**: Implement pixel-level hydrogen exclusion raster and per-zone availability CSV, following the same pattern as `build_nuclear_smr_exclusions.py`. Reuse shared foundation helper functions (protected areas, land cover, flooding, groundwater SPZ, coastal erosion, ALC BMV, Green Belt). Hydrogen-specific differences from SMR:
  - **No Scotland ban** — hydrogen is not politically excluded from Scotland
  - **No ONR population criterion** — hydrogen uses simpler population density threshold from `hydrogen.siting_constraints` config
  - **No COMAH/gas pipe buffer** — hydrogen isn't a radiological hazard
  - **Airfield FRZ**: disabled for hydrogen (no safety case)
  - **Water availability**: critical for electrolysis (placeholder disabled by default, same pattern as SMR)
  - Multi-technology output if electrolysis and h2_turbine configs differ
- **File(s)**: `scripts/land/build_hydrogen_exclusions.py` (new)
- **Done when**: Script runs standalone. Output exclusion raster `h2_exclusions_{network_model}.tif` and availability CSV `h2_availability_{network_model}.csv`. All zones have fractions 0.0–1.0. Scotland zones have non-zero values (hydrogen NOT banned from Scotland).

### 7.2 Add `build_hydrogen_exclusions` rule to `rules/land_constraints.smk` Section B

- **Time**: 30min
- **Action**: Add rule to `rules/land_constraints.smk` Section B (per-technology exclusions), following the pattern of `build_nuclear_smr_exclusions`. Uses `{network_model}` wildcard. Inputs: foundation rasters + zone shapes. Outputs: exclusion raster + availability CSV. Config from `hydrogen.siting_constraints` in `defaults.yaml`.
- **File(s)**: `rules/land_constraints.smk` (existing — add to Section B)
- **Done when**: Dry-run succeeds. DAG shows correct dependencies: foundation rasters → build_hydrogen_exclusions.

### 7.3 Write unit tests for hydrogen exclusion logic

- **Time**: 1hr
- **Action**: Create `tests/unit/test_build_hydrogen_exclusions.py`, following the pattern of `test_build_nuclear_smr_exclusions.py`. Test: all foundation layer helpers (enable/disable via config), no Scotland ban (Scotland zones should have non-zero availability unlike SMR), water availability placeholder (disabled → all pass, enabled → NotImplementedError), config-driven layer selection. Use synthetic rasters.
- **File(s)**: `tests/unit/test_build_hydrogen_exclusions.py` (new)
- **Done when**: `pytest tests/unit/test_build_hydrogen_exclusions.py -v` passes. At least 30 test cases covering all exclusion layers and config toggles.

### 7.4 Run hydrogen branch with real data (manual test)

- **Time**: 30min
- **Action**: Run `snakemake resources/land/h2_exclusions_Zonal.tif resources/land/h2_availability_Zonal.csv --cores 4`. Verify: Scotland zones have non-zero availability (unlike SMR). England/Wales zones have sensible fractions. Availability fractions are generally higher than SMR (fewer exclusion layers).
- **File(s)**: No files modified — manual validation
- **Done when**: Output files exist. Scotland zones not excluded. Raster ready for QGIS review.

---

## Phase 8: Renewables Branch

> **Goal**: Implement renewable land/marine availability and technical potential calculation
> (most complex — requires atlite ExclusionContainer + marine KRA data).
> **Finish before**: Phase 9.
>
> **Key design decisions**:
> - Section B rules have NO `{scenario}` wildcard — outputs are scenario-independent spatial data
> - `build_offshore_exclusions` ONLY builds a binary exclusion raster (not KRA processing or technical potential)
> - KRA processing (fixed & floating, all GB) happens in `build_offshore_available_kra` (separated from availability matrix for modularity)
> - `build_offshore_available_kra` outputs a GeoPackage with per-zone KRA fragments including TG, cost_tier, carrier, distance, available_area
> - TG classifications inform `capital_cost` but do NOT constrain optimiser deployment ordering
> - Existing/committed capacity (REPD + AR1–AR7) is loaded by `integrate_renewable_generators.py`,
>   NOT by land_constraints.smk. The subtraction `p_nom_max = potential - existing` happens
>   in `apply_land_constraints` (solve.smk). Existing capacity subtracted from cheapest tier first.
> - **Offshore wind spatial distribution** (see `.claude/plans/snoopy-shimmying-crayon.md`):
>   - Each (zone, carrier, cost_tier) becomes a triplet: offshore bus + Generator + OFTO Link
>   - Generator capital_cost = base_capex × TG_multiplier (turbine/foundation)
>   - Link capital_cost = cable + substation/converter cost (distance-dependent, AC/DC)
>   - AC if KRA centroid < 50km from coast, DC if >= 50km
>   - Connection losses applied as p_max_pu derating on generators (Link efficiency=1.0)
>   - FES offshore target enforced as single combined total (fixed + floating); optimiser decides split
>   - Four carriers: `offwind-fixed-ac`, `offwind-fixed-dc`, `offwind-float-ac`, `offwind-float-dc`
>   - Up to 5 cost tiers per carrier per zone (F1-F3b for fixed, FL1-FL3b for floating)

### 8.1 Write `scripts/land/build_offshore_exclusions.py` - DONE, NOT VALIDATED

- **Time**: 1.5hr
- **Action**: Implement per workflow Section 4.2. Load exclusion zones datasets (covering 7 exclusion types: MPAs, shipping Q90, O&G fields, CCS licences, gas storage, tidal/wave plan options, marine mining). Merge all exclusion geometries, reproject to EPSG:27700, rasterise to canonical GB grid as single-band binary raster (1 = excluded, 0 = available).
- **Input data**:
  - `data/land/marine/gb_eez.gpkg` (GB EEZ zone)
  - `data/land/marine/shipping_density_eu.geotiff` (shipping density)
  - `data/land/marine/offwind_protected_areas_gb.gpkg` (GB offshore MPAs)
  - `data/land/marine/offshore_licensed_ccs_ew.gpkg` (E&W CCS licensed sites)
  - `data/land/marine/offshore_licensed_ccs_scotland.gpkg` (Scottish CCS licensed sites)
  - `data/land/marine/offshore_gas_storage_sites_gb.gpkg` (GB gas storage licensed sites)
  - `data/land/marine/offshore_o&g_zones_gb.gpkg` (GB O&G licensed zones)
  - `data/land/marine/marine_mining_sites_gb.gpkg` (GB marine mining licensed sites)
  - `data/land/marine/marine_aggregates_sites_gb.gpkg` (GB marine aggregates licensed sites)
  - `data/land/marine/wave_licensed_sites_scotland.gpkg` (Scottish wave licensed sites)
  - `data/land/marine/wave_licensed_sites_ew.gpkg` (E&W wave licensed sites)
  - `data/land/marine/tidal_licensed_sites_scotland.gpkg` (Scottish tidal licensed sites)
  - `data/land/marine/tidal_licensed_sites_ew.gpkg` (E&W tidal licensed sites)
- **File(s)**: `scripts/land/build_offshore_exclusions.py` (new)
- **Done when**: Single binary GeoTIFF `resources/land/offshore_exclusions.tif` produced. Raster aligns with canonical GB grid (same CRS, resolution, extent as foundation rasters). Exclusion zones are rasterised as 1, all other marine area as 0. Visual sanity check confirms MPAs and shipping routes appear as excluded.

#### Code Logic

1. for each vector dataset, clip to GB EEZ via spatial intersection; for shipping density raster, clip via rasterio.mask.mask() before applying Q90 threshold i.e. gb data only
2. shipping density filtered to Q90 threshold — routes at or above the 90th percentile are treated as exclusion zones
3. merge all exclusion geometries, reproject to EPSG:27700, rasterise to canonical GB grid as single-band binary raster

### 8.1b Write `scripts/land/build_offshore_available_kra.py` - DONE, VALIDATED

- **Time**: 3hr
- **Action**: Process offshore wind KRAs by subtracting marine exclusion zones, classifying TG cost tiers, calculating distance-to-coast, and intersecting with zone boundaries. Separated from `build_onshore_renewable_availability.py` for modularity — KRA data is fundamentally vector and needs TG attributes preserved as a reusable GeoPackage for downstream cost modelling.
- **Excluded inputs**: CES leases (existing capacity — handled downstream in `apply_land_constraints`), INTOG application areas (excluded as data input).

**Processing steps:**

1. Load Fixed KRA (13 polygons) and Floating KRA (6 polygons) geojsons, reproject EPSG:4326 → EPSG:27700
2. Parse `Rating` attribute → `tg_class` (e.g. "Technology Group 7B" → "TG-7B")
3. Map TG → cost_tier and capex_multiplier:
   - **Fixed**: F1 (1.00) ← TG-1,2A,2B,4A; F2a (1.10) ← TG-3A,3B,4B; F2b (1.15) ← TG-5A,5B; F3a (1.20) ← TG-6A,6B; F3b (1.30) ← TG-7A,7B
   - **Floating**: FL1 (1.00) ← TG-1; FL2a (1.10) ← TG-2,3; FL2b (1.15) ← TG-4; FL3a (1.20) ← TG-5; FL3b (1.30) ← TG-6
4. Derive coastline from union of onshore zone boundaries (excluding DOGGER_BANK, HORNSEA, EAST_ANGLIA)
5. Calculate centroid distance to nearest coastline via `shapely.ops.nearest_points`
6. Classify carrier: offwind-fixed-ac (<50km), offwind-fixed-dc (≥50km), offwind-float-ac (<50km), offwind-float-dc (≥50km)
7. Intersect KRA polygons with zone boundaries (`gpd.overlay`)
8. For each fragment, sample exclusion raster (`rasterio.mask.mask`) → count available pixels → `available_area_km2`
9. Compute area-weighted centroids from available pixel coordinates
10. Drop fully excluded fragments, write GeoPackage

**Output schema** — `resources/land/offshore_available_kra_{network_model}.gpkg`:
- `kra_name`, `tg_class`, `cost_tier`, `capex_multiplier`, `kra_type`, `carrier`, `connection_type`, `distance_to_coast_km`, `zone_name`, `available_area_km2`, `centroid_x`, `centroid_y`, `geometry`

- **Input data**: `offshore_exclusions.tif` + Fixed/Floating KRA geojsons + zone shapes
- **File(s)**: `scripts/land/build_offshore_available_kra.py` (new)
- **Test file**: `tests/unit/test_build_offshore_available_kra.py` (51 tests)
- **Done when**: Output GeoPackage has one record per KRA-zone intersection fragment. All 13+6 KRA Rating values parsed correctly. Carrier classification correct (fixed/floating × AC/DC). Fragments with zero available area dropped. Visual sanity check in QGIS confirms zone boundaries align.
- **Validated**: 2026-03-22. 41 fragments across 12 zones, 5,633.7 km² total. 5 KRA polygons fully excluded (TG-4B, TG-5A fixed; TG-2, TG-4, TG-6 floating). 51 unit tests passing.

### 8.2 Write `scripts/land/build_onshore_renewable_availability.py` — DONE, VALIDATED, REFACTORED (2026-03-29)

- **Time**: 3hr (onshore only — offshore processing now in `build_offshore_available_kra`)
- **Action**: Core exclusion + availability calculation for **onshore technologies only** (onwind, solar). Overlay foundation rasters with technology-specific buffers and exclusion rules from `config/defaults.yaml`.

**Onshore processing (onwind, solar):**

For each onshore technology, the script reads config from `defaults.yaml` (e.g. `land_constraints.onwind`) and applies 8 exclusion layers in order:

1. **Protected areas** (4-band `protected_areas_gb.tif`): per-tier hard exclusion
2. **FRZ exclusion zones** (2-band `airfields_gb.tif`): pre-buffered FRZ circles
3. **Land cover** (`land_cover_gb.tif`): exclude specific UK LCM 2024 habitat codes with optional per-code buffers
4. **Flooding risk** (`flooding_risk_gb.tif`): hard exclusion
5. **Groundwater SPZ** (3-band `groundwater_spz_ew.tif`): exclude bands per config
6. **Coastal change** (`coastal_erosion_gb.tif`): hard exclusion
7. **ALC BMV** (`alc_bmv_gb.tif`): BMV agricultural land exclusion
8. **Green Belt** (`green_belt_gb.tif`): Green Belt exclusion

Buffering uses `scipy.ndimage.binary_dilation` with a disk structuring element.

- **Input data**: 8 foundation rasters + `scotland_mask_{network_model}.tif` + zone shapes
- **File(s)**: `scripts/land/build_onshore_renewable_availability.py`
- **Test file**: `tests/unit/test_build_onshore_renewable_availability.py` (17 passing, 14 pre-existing failures from old function signature — tracked for fix)
- **Done when**: ~~Output CSV `availability_matrix_{network_model}.csv`~~ **DONE & REFACTORED**: Output raster `onshore_renewable_exclusions_{network_model}.tif` (2-band: onwind, solar) and CSV `onshore_renewable_availability_{network_model}.csv` with availability values 0.0–1.0 per zone.
- **Validated**: 2026-03-22 (original). 2026-03-29 (refactored — rule renamed to `build_onshore_renewable_exclusions`, multi-band exclusion raster added, CSV path renamed).

### 8.3 ~~Write `scripts/land/calculate_renewable_potential.py`~~ → RENAMED `calculate_technical_potential.py` — DONE, VALIDATED, REFACTORED (2026-03-29)

- **Time**: 2hr
- **Action**: Convert availability fractions (onshore) and available areas (offshore) into technical potential (MW) per zone per technology using capacity densities from `defaults.yaml`.

**Onshore** (onwind, solar):
  - `p_nom_max = availability_fraction × zone_area_km2 × capacity_density`
  - Zone areas from `availability_matrix_{network_model}.csv` (`area_km2` column, embedded by exclusion rules)

**Offshore** (offwind-fixed-ac/dc, offwind-float-ac/dc):
  - Reads `offshore_available_kra_{network_model}.gpkg` (from `build_offshore_available_kra`)
  - Groups by `(zone_name, carrier, cost_tier)`
  - Per group: sum `available_area_km2`, area-weighted mean `distance_to_coast_km` and centroid
  - `p_nom_max = available_area_km2 × capacity_density`
  - `connection_type` and `carrier` used as-is from KRA data (already classified upstream)

**Capacity densities** (from `defaults.yaml`):
  - onwind: 3.0, solar: 50.0, offwind-fixed-ac: 6.0, offwind-fixed-dc: 4.0, offwind-float-ac: 3.0, offwind-float-dc: 2.0 MW/km²
  - Floating density derived from carrier name: `-ac` → 3.0, `-dc` → 2.0

**Output CSV columns**: `zone_name`, `carrier`, `cost_tier`, `p_nom_max_mw`, `capacity_density`, `capex_multiplier`, `connection_type`, `distance_to_coast_km`, `centroid_x`, `centroid_y`. Onshore rows have empty/NaN offshore-specific fields.

- **File(s)**: `scripts/land/calculate_technical_potential.py` (renamed from `calculate_renewable_potential.py` on 2026-03-29)
- **Test file**: `tests/unit/test_calculate_renewable_potential.py` (25 tests — test file not yet renamed)
- **Done when**: ~~Output CSV `renewable_technical_potential_{network_model}.csv`~~ **DONE & REFACTORED**: Output CSV `technical_potential_{network_model}.csv` has rows for all carriers including SMR. `p_nom_max_mw ≥ 0` for all rows.
- **Validated**: 2026-03-23 (original). 2026-03-29 (refactored — renamed, reads unified availability matrix with `area_km2`, removed `zone_stats` dependency, added SMR + hydrogen config routing). 92 rows across 7 carriers (onwind 86 GW, solar 3.6 TW, SMR 47 GW, offshore 21 GW).

### 8.4 Add renewable constraint rules to `rules/land_constraints.smk` Section B - DONE

- **Time**: 30min
- **Action**: Added 3 rules to `rules/land_constraints.smk` Section B: `build_offshore_exclusions`, `build_offshore_available_kra`, `build_onshore_renewable_exclusions` (renamed from `build_onshore_renewable_availability` on 2026-03-29). `calculate_renewable_potential` moved to Section D and renamed `calculate_technical_potential`. All use `{network_model}` wildcard.
- **File(s)**: `rules/land_constraints.smk`
- **Done when**: DAG chains: foundation rasters + offshore exclusions → offshore KRA → renewable potential; foundation rasters → onshore availability → renewable potential.

### 8.5 Write unit tests for offshore exclusions and availability matrix

- **Time**: 1.5hr
- **Action**: Create `tests/unit/test_offshore_exclusions.py` and `tests/unit/test_availability_matrix.py`. For offshore exclusions: test that exclusion geometries are correctly rasterised, output is binary (0/1 only), CRS matches canonical grid. For availability matrix: test exclusion logic (fully excluded zone → 0.0 availability), no-exclusion zone → 1.0, partial exclusion produces intermediate values, offshore zones use KRA boundaries, onshore zones use foundation layers. Mock the ExclusionContainer for fast testing.
- **File(s)**: `tests/unit/test_offshore_exclusions.py` (new), `tests/unit/test_availability_matrix.py` (new)
- **Done when**: `pytest tests/unit/test_offshore_exclusions.py tests/unit/test_availability_matrix.py -v` passes.

### 8.6 Write unit tests for renewable potential calculation

- **Time**: 30min
- **Action**: Create `tests/unit/test_renewable_potential.py`. Test: p_nom_max = availability × area × capacity_density. Zero availability → zero p_nom_max. Negative values impossible. Capacity densities match config values (offwind-ac: 8.0, offwind-float: 6.0).
- **File(s)**: `tests/unit/test_renewable_potential.py` (new)
- **Done when**: `pytest tests/unit/test_renewable_potential.py -v` passes.

---
## Unified Pipeline Integration Tasks (added 2026-03-29)

> These tasks integrate the per-technology exclusion outputs into a single unified availability matrix and technical potential calculation.

### 8.7 Refactor `build_onshore_renewable_availability` → `build_onshore_renewable_exclusions` — **COMPLETE** (2026-03-29)

- **Time**: 30min
- **Action**: Renamed rule to `build_onshore_renewable_exclusions`. Added multi-band exclusion raster output (Band 1 = onwind, Band 2 = solar) via `write_geotiff()`. Updated CSV output path from `availability_matrix_{nm}.csv` to `onshore_renewable_availability_{nm}.csv`. Updated `calculate_renewable_potential` input path to match. Script file not renamed (preserves git history).
- **File(s)**: `scripts/land/build_onshore_renewable_availability.py` (modified), `rules/land_constraints.smk` (rule renamed + outputs updated)
- **Done when**: ~~Rule outputs both raster and CSV.~~ **DONE**: `onshore_renewable_exclusions_Zonal.tif` (2.5 MB, 2-band) and `onshore_renewable_availability_Zonal.csv` produced. `calculate_renewable_potential` DAG still chains correctly. 17/31 pre-existing tests pass (14 failures are pre-existing old function signature issue).

### 8.8 Create `build_availability_matrix` rule — **COMPLETE** (2026-03-29)

- **Time**: 30min
- **Action**: Created simple merge rule that reads per-technology availability CSVs (onshore renewables, SMR) and combines into `availability_matrix_{network_model}.csv`. Hydrogen input is conditional (skipped when not available). Onshore CSV's `area_km2` column flows through as the base. SMR zones not in onshore (shouldn't happen) get 0.0.
- **File(s)**: `scripts/land/build_availability_matrix.py` (new), `rules/land_constraints.smk` (rule added to Section D)
- **Done when**: ~~Output CSV has columns: zone, onwind, solar, smr.~~ **DONE**: Output CSV has columns: `zone, area_km2, onwind, solar, smr`. 20 zones. Scottish zones show 0.0 for SMR. `area_km2` column enables self-contained technical potential calculation.

### 8.9 Rename `calculate_renewable_potential` → `calculate_technical_potential` — **COMPLETE** (2026-03-29)

- **Time**: 30min
- **Action**: Renamed rule and script. Extended to read unified availability matrix (all technologies) with embedded `area_km2`. Removed `zone_stats` input — `area_km2` is now in the availability matrix. Added `nuclear_config` and `hydrogen_config` params for SMR/H2 capacity density lookup. Old script file (`calculate_renewable_potential.py`) retained for test compatibility.
- **File(s)**: `scripts/land/calculate_technical_potential.py` (new, based on `calculate_renewable_potential.py`), `rules/land_constraints.smk` (rule renamed, moved to Section D, `zone_stats` input removed)
- **Done when**: ~~Output CSV includes all technologies.~~ **DONE**: Output CSV `technical_potential_Zonal.csv` has 92 rows across 7 carriers (onwind 86 GW, solar 3.6 TW, SMR 47 GW, offshore 21 GW). SMR capacity_density = 1.0 MW/km² (placeholder).

---
## TODO: check availability matrix & technical potential are calculated & integrated for all relevant technologies
[renewables-land-constraints_workflow_plan_PYPSA-GB](renewables-land-constraints_workflow_plan_PYPSA-GB.md) [nuclear-land-restrictions_workflow_plan](nuclear-land-restrictions_workflow_plan.md)
## Phase 9: Generator & Hydrogen Updates + Constraint Application

> **Goal**: Three sub-goals: (A) update generator scripts to add extendable future generators, (B) update hydrogen script to add H₂ demand and extendable components, (C) add `apply_land_constraints` rule to `solve.smk` to set `p_nom_max` from land constraint outputs.
> **Finish before**: Phase 10.
> **Architecture**: There is no `land_integration.smk`. The `apply_land_constraints` rule and its helper functions live in `solve.smk`. The constraint application script lives at `scripts/solve/apply_land_constraints.py`. See workflow doc Sections 4.4, 4.5, and 4.6.

### Part A: Generator Integration Updates

These tasks update the **existing** generator scripts so that future scenarios add extendable generators instead of fixed-capacity generators. The Snakemake rules in `generators.smk` do not change — only the Python scripts they call.

### 9.1 Update `scripts/generators/integrate_renewable_generators.py` for extendable future generators

- **Time**: 4hr
- **Action**: Modify the existing script so that for **future scenarios** (modelled_year > 2024):
  - **Onshore wind, solar**: added as extendable generators at each zone (`p_nom=0`, `p_nom_extendable=True`, `p_nom_max` left unconstrained — set later by `apply_land_constraints`). Weather profiles still applied per-bus for capacity factors.
  - **Offshore wind** (NEW — bus + generator + OFTO link triplets): Add new function `create_offshore_wind_components(network, potential_df, config)` that reads `renewable_technical_potential.csv` and for each (zone, carrier, cost_tier) row creates:
    1. **Offshore bus** at KRA centroid (`offwind_bus_{zone}_{carrier_short}_{tier}`, coords from CSV)
    2. **Extendable Generator** on offshore bus: `capital_cost = base_capex × capex_multiplier` (turbine/foundation only), `p_max_pu = weather_profile × loss_factor` (connection loss derating)
    3. **Extendable OFTO Link** from offshore bus to zone bus: `capital_cost = cable + converter cost` (distance-dependent, AC/DC from config `offshore_connection`), `carrier = AC or DC`, `efficiency = 1.0`, `p_min_pu = 0` (unidirectional)
  - Loss derating: AC = `1 - (ac_loss_per_km × distance)`, DC = `(1 - dc_converter_loss)² × (1 - dc_loss_per_km × distance)`
  - New carrier definitions needed: `offwind-ac` and `offwind-float` in `carrier_definitions.py`
  - Marine renewables (tidal, wave) and hydro remain unchanged (not spatially optimised)
  - **Historical scenario behaviour is completely unchanged** (REPD site data, fixed capacities)
  See workflow doc Section 4.4 for full details.
- **File(s)**: `scripts/generators/integrate_renewable_generators.py` (existing — modify), `scripts/utilities/carrier_definitions.py` (add 2 carriers)
- **Done when**: Future scenario has offshore buses, generators, and OFTO links. Bus count = generator count = link count for offshore (1:1:1 ratio). Generator capital_cost reflects TG multiplier. Link capital_cost reflects distance-dependent AC/DC cable cost. Historical scenario generators are unchanged. Network builds successfully for both historical and future scenarios.

### 9.2 Update `scripts/generators/integrate_thermal_generators.py` for extendable SMR

- **Time**: 1hr
- **Action**: Modify the existing script so that for **future scenarios**: SMR (nuclear-small) is added as extendable generators at all zones (`p_nom=0`, `p_nom_extendable=True`, `p_nom_max` set later). CCGT, OCGT, biomass, oil, nuclear-large remain restricted to historical site locations with endogenously determined capacity. See workflow doc Section 4.4. **Historical scenario behaviour is completely unchanged.**
- **File(s)**: `scripts/generators/integrate_thermal_generators.py` (existing — modify)
- **Done when**: Future scenarios have extendable SMR generators at all zones. Historical scenarios are unchanged. Network builds successfully.

### 9.3 Write unit tests for extendable generator changes

- **Time**: 1hr
- **Action**: Add tests to existing generator test files (or create `tests/unit/test_extendable_generators.py`). Test: (a) future scenario onshore wind generators have `p_nom_extendable=True` and `p_nom=0`, (b) historical scenario generators have `p_nom > 0` and `p_nom_extendable=False`, (c) SMR generators are present at all zones for future scenarios, (d) marine/hydro renewables are NOT extendable regardless of scenario year.
- **File(s)**: `tests/unit/test_extendable_generators.py` (new or extend existing)
- **Done when**: `pytest tests/unit/test_extendable_generators.py -v` passes. At least 6 test cases.

### Part B: Hydrogen Integration Updates

These tasks update the **existing** hydrogen script to add H₂ demand loads and make electrolysers/turbines extendable. The Snakemake rule in `hydrogen.smk` may need minor input/params changes but the structure is preserved.

### 9.4 Update `scripts/hydrogen/add_hydrogen_system.py` for H₂ demand and extendable components

- **Time**: 2hr
- **Action**: Modify the existing script to: (a) add hydrogen demand loads at H₂ buses (industrial/heating H₂ demand from FES data), (b) change electrolysers to `p_nom_extendable=True` with `p_nom_max` left unconstrained (set later by `apply_land_constraints`), (c) change H₂ turbines to `p_nom_extendable=True` with `p_nom_max` left unconstrained. The existing zonal H₂ network (38 buses, 40 pipeline links, 5 storage sites) is preserved as-is. **Historical scenarios remain a no-op** (≤2024). See workflow doc Section 4.5.
- **File(s)**: `scripts/hydrogen/add_hydrogen_system.py` (existing — modify)
- **Done when**: Future scenarios have H₂ demand loads at H₂ buses, extendable electrolysers, extendable H₂ turbines. Historical scenarios are unchanged. Network builds successfully.

### 9.5 Update `rules/hydrogen.smk` if needed for new inputs

- **Time**: 30min
- **Action**: Check if the hydrogen rule needs any additional inputs or params for H₂ demand data. If FES hydrogen demand data is needed as an input, add it to the rule's `input:` section. The rule structure and output filename remain the same.
- **File(s)**: `rules/hydrogen.smk` (existing — minor update if needed)
- **Done when**: `snakemake -n` dry-run succeeds for a future scenario. Hydrogen rule has any needed new inputs.

### 9.6 Write unit tests for hydrogen updates

- **Time**: 1hr
- **Action**: Add tests for the hydrogen changes. Test: (a) future scenario has H₂ demand loads, (b) electrolysers are `p_nom_extendable=True`, (c) H₂ turbines are `p_nom_extendable=True`, (d) historical scenario is a no-op (no H₂ components), (e) existing pipeline links and storage are unchanged.
- **File(s)**: `tests/unit/test_hydrogen_updates.py` (new or extend existing)
- **Done when**: `pytest tests/unit/test_hydrogen_updates.py -v` passes. At least 5 test cases.

### Part C: Constraint Application in `solve.smk`

These tasks add the `apply_land_constraints` rule and helpers to `solve.smk`. This is where land constraint CSV outputs are applied to network `p_nom_max` values, FES annual capacity targets are stored as network metadata (enforced via `extra_functionality` at solve time), and carbon budget constraints (CO₂e limits per modelled year from config) are added via `GlobalConstraint`.

### 9.7 Add helper functions to `rules/solve.smk`

- **Time**: 30min
- **Action**: Add two helper functions to `solve.smk`: (a) `get_land_constraint_inputs(wildcards)` — returns a dict of conditional input files based on which constraint modules are enabled in config (renewable potential CSV, nuclear eligibility CSV, H₂ eligibility CSV). Returns empty dict when all disabled. (b) `_get_pre_constraint_network(scenario_id)` — returns the correct input network path (clustered or unclustered). Use `unpack()` pattern per template compliance T13. See workflow doc Section 4.6 for exact function signatures.
- **File(s)**: `rules/solve.smk` (existing — add functions)
- **Done when**: Functions are syntactically correct. `get_land_constraint_inputs` returns empty dict when all disabled, returns correct paths when individual modules are enabled. Handles all 7 enable/disable combinations.

### 9.8 Add `apply_land_constraints` rule to `rules/solve.smk`

- **Time**: 30min
- **Action**: Add the `apply_land_constraints` rule to `solve.smk`, placed **before** the existing `finalize_network` rule. The rule **always runs**: when all constraint modules are disabled it copies the input network unchanged (passthrough), so `finalize_network` always reads `_constrained.nc`. Uses `unpack(get_land_constraint_inputs)` for conditional inputs. Input network from `_get_pre_constraint_network()`. Output: `{scenario}_constrained.nc`. Script at `scripts/solve/apply_land_constraints.py`. Follow STYLE_GUIDE fully: docstring, wildcard_constraints, message, log, benchmark, conda. See workflow doc Section 4.6 for full rule spec.
- **File(s)**: `rules/solve.smk` (existing — add rule)
- **Done when**: Rule follows STYLE_GUIDE. Dry-run succeeds with land constraints enabled. Dry-run also succeeds with all constraints disabled.

### 9.9 Update `finalize_network` rule input in `rules/solve.smk`

- **Time**: 15min
- **Action**: Update the `finalize_network` rule's input to **always** read `{scenario}_constrained.nc` (no conditional lambda). Because `apply_land_constraints` always runs (passthrough when disabled), this keeps the DAG linear and deterministic. The `finalize_network` output, params, and script are unchanged. See workflow doc Section 4.6.
- **File(s)**: `rules/solve.smk` (existing — modify `finalize_network` input)
- **Done when**: Dry-run shows `apply_land_constraints → finalize_network → solve_network` chain regardless of whether constraints are enabled or disabled.

### 9.10 Write `scripts/solve/apply_land_constraints.py`

- **Time**: 3hr
- **Action**: Implement per workflow Section 4.6. Load network, read whichever constraint outputs are available based on `snakemake.params` flags. Processing steps:
  (1) If renewable constraints enabled: set `p_nom_max` on onshore renewable generators from `renewable_technical_potential.csv` based on zone and carrier.
  (2) **Offshore wind existing capacity subtraction** (NEW): For each (zone, carrier) in [offwind-ac, offwind-float], subtract existing/committed capacity (REPD + AR1-AR7) from `p_nom_max`, starting from the **cheapest cost tier first** (conservative: best sites occupied first). IMPORTANT: must update `p_nom_max` on BOTH the generator AND its matched OFTO link to maintain physical coupling.
  (3) If nuclear constraints enabled: set `p_nom_max` on SMR generators from `smr_candidates.csv` (eligible zones get max_capacity, ineligible zones get 0).
  (4) If hydrogen constraints enabled: set `p_nom_max` on electrolysers/H₂ turbines from `zone_eligibility.csv` (ineligible zones get 0).
  (5) Store FES annual capacity targets as `n.meta["fes_capacity_targets"]` dict. For offshore wind, store a **single combined target** (`offshore_wind_total` = FES fixed + floating) — optimiser decides the split (enforced at solve time via `extra_functionality`, NOT `GlobalConstraint`).
  (6) Add carbon budget constraint — look up CO₂e limit (MtCO₂e/yr) for the modelled year from config `carbon_budget.annual_limits`, convert to tonnes, add as `GlobalConstraint` with `type="primary_energy"` and `carrier_attribute="co2_emissions"`. Warn if carbon budget enabled but no limit defined for the modelled year.
  Use `load_network()`/`save_network()` from `scripts/utilities/network_io.py`. Follow SCRIPT_PATTERNS.md fully. See workflow doc Section 4.6 pseudocode.
- **File(s)**: `scripts/solve/apply_land_constraints.py` (new)
- **Done when**: Script runs standalone. When all modules disabled, network passes through unchanged (passthrough). When enabled: onshore `p_nom_max` values set, offshore `p_nom_max` values set on both generators AND OFTO links (with existing capacity subtracted cheapest-first), no generator has negative `p_nom_max`, generator p_nom_max matches its link p_nom_max for offshore. FES capacity targets stored in `n.meta["fes_capacity_targets"]` with `offshore_wind_total` key. Carbon budget `GlobalConstraint` added when config has entry for modelled year.

### 9.11 Write unit tests for `apply_land_constraints.py`

- **Time**: 1hr
- **Action**: Create `tests/unit/test_apply_land_constraints.py`. Test: (a) all modules disabled produces unchanged network (passthrough), (b) renewable constraints set correct `p_nom_max` from CSV, (c) nuclear constraints zero-out SMR in ineligible zones, (d) hydrogen constraints zero-out electrolysers in ineligible zones, (e) FES capacity targets stored in `n.meta["fes_capacity_targets"]` when renewable constraints enabled, (f) carbon budget `GlobalConstraint` added when config has `annual_limits` entry for modelled year, (g) carbon budget constraint NOT added when modelled year has no entry in `annual_limits`, (h) carbon budget limit value correctly converted from MtCO₂e to tonnes, (i) multiple modules enabled simultaneously work correctly. Use `minimal_network` fixture from conftest.py.
- **File(s)**: `tests/unit/test_apply_land_constraints.py` (new)
- **Done when**: `pytest tests/unit/test_apply_land_constraints.py -v` passes. At least 9 test cases.

### 9.12 Verify full pipeline dry-run with all updates

- **Time**: 15min
- **Action**: Run `snakemake -n` dry-run for a future scenario with all land constraints enabled. Verify the complete DAG: `land_constraints.smk` (foundation → renewables → nuclear → hydrogen) runs in parallel with the network integration pipeline (`demand → generators → storage → hydrogen → interconnectors`), converging at `apply_land_constraints → finalize_network → solve_network`.
- **File(s)**: No files modified — validation only
- **Done when**: Dry-run completes without errors. DAG shows parallel tracks converging at `apply_land_constraints`. No circular dependencies. No missing inputs.

---

## Phase 10: End-to-End Testing

> **Goal**: Verify the full pipeline works from raw data through to solved network.

### 10.1 Create a minimal test scenario with land constraints enabled

- **Time**: 30min
- **Action**: Add a test scenario to `config/scenarios.yaml` (e.g., `Test_Land_Constraints`) with `land_constraints.enabled: true`, `nuclear.siting_constraints.enabled: true`, `hydrogen.siting_constraints.enabled: true`. Use a small solve period (1 week) to keep runtime manageable.
- **File(s)**: `config/scenarios.yaml`
- **Done when**: Scenario definition is valid per `config_loader.py --validate`.

### 10.2 Run full pipeline dry-run

- **Time**: 15min
- **Action**: Run `snakemake -n {scenario}_solved.nc` with the test scenario. Verify the DAG includes all land constraint rules in the correct order.
- **File(s)**: No files modified
- **Done when**: Dry-run completes without errors. DAG shows two parallel tracks converging: `land_constraints.smk` (GIS pipeline) runs in parallel with network integration pipeline (`demand → generators → storage → hydrogen → interconnectors`), both converging at `apply_land_constraints` in `solve.smk`, then `finalize_network → solve_network`. No circular dependencies.

### 10.3 Run full pipeline (real execution)

- **Time**: 2hr (estimated 40-70 min compute + monitoring)
- **Action**: Run `snakemake {scenario}_solved.nc --cores 4` with real data. Monitor for errors. Check all intermediate outputs exist and have expected structure.
- **File(s)**: No files modified — execution only
- **Done when**: Pipeline completes without errors. All output files exist in `resources/land/`, `resources/generators/`, `resources/hydrogen/`. Solved network has `p_nom_max` values set on constrained generators.

### 10.4 Validate p_nom_max values and constraints in solved network

- **Time**: 30min
- **Action**: Load the solved network and verify: (a) renewable generators have `p_nom_max` < infinity, (b) SMR generators in Scottish zones have `p_nom_max = 0`, (c) non-Scottish eligible zones have `p_nom_max > 0` for SMR, (d) hydrogen electrolysis in ineligible zones has `p_nom_max = 0`, (e) total constrained capacity is less than unconstrained for renewables, (f) total installed renewable capacity per carrier equals FES annual target (enforced via `extra_functionality` at solve time — verify `n.meta["fes_capacity_targets"]` matches solved p_nom sums), (g) carbon budget `GlobalConstraint` exists with correct CO₂e limit for the modelled year (e.g., 0 for 2050, 75 MtCO₂e for 2040).
- **File(s)**: No files modified — validation script or notebook
- **Done when**: All 7 checks pass. Results are documented (screenshot or log output).

### 10.5 Run full test suite

- **Time**: 30min
- **Action**: Run `pytest tests/ -v --tb=short` to ensure no existing tests are broken by the new code. Then run all new tests specifically: `pytest tests/unit/test_build_protected_areas_raster.py tests/unit/test_calculate_zone_statistics.py tests/unit/test_nuclear_eligibility.py tests/unit/test_hydrogen_eligibility.py tests/unit/test_availability_matrix.py tests/unit/test_renewable_potential.py tests/unit/test_extendable_generators.py tests/unit/test_hydrogen_updates.py tests/unit/test_apply_land_constraints.py -v`.
- **File(s)**: No files modified
- **Done when**: All tests pass. Zero failures. Zero errors.

### 10.6 Run pipeline with all constraints disabled (regression test)

- **Time**: 30min
- **Action**: Run the standard pipeline with `land_constraints.enabled: false` (default). Verify the output is identical to a pre-land-constraints run — the new code should have zero impact when disabled.
- **File(s)**: No files modified
- **Done when**: Solved network with constraints disabled is byte-identical (or numerically identical) to a baseline solved network without any land constraint code in the pipeline.

# Land Constraints Implementation Task List

**Created**: 2026-02-08
**Updated**: 2026-03-18
**Source**: `docs/planning/workflow_plans/unified_land_constraints_workflow.md` (v2.0)
**Status**: Pre-implementation review complete
**Estimated Time: 3 weeks**
**Estimated Completion Date: [[2026-02-27]]**
**Blockers: HPC Approval**

---
## Modifications Made

### [data_sources_land](data_sources_land.md)

Link: [DATA_PATTERNS](DATA_PATTERNS.md)
- Checklist for Data Processing

1. changed file structure
2. created new `scripts/setup/prepare_raw_data.py`

**So the split is:**

| Concern                                  | Solution                       | Location                               |
| ---------------------------------------- | ------------------------------ | -------------------------------------- |
| Extract & rename raw zips                | Standalone setup script        | `scripts/setup/prepare_raw_data.py`    |
| Content validation (CRS, schema, bounds) | snakemake rule helper function | see below section land_constraints.smk |
| Missing file detection                   | Snakemake rule helper function | see below section land_constraints.smk |



### land_constraints.smk

Looking at how `renewables.smk` does it — `_get_available_cutouts()` as a helper function that runs at DAG evaluation time and fails fast with clear error messages — that's a better pattern for your land data validation than a separate utility module.

So follow the same structure in `land_constraints.smk`:

```python
# ══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════

land_data_path = f"{data_path}/land"

def _validate_land_data_ready():
    """Check that raw data has been extracted and renamed. Fail fast."""
    from pathlib import Path

    expected = {
        "environment": ["gb_sac.gpkg", "gb_spa.gpkg", "gb_ramsar.gpkg",
                        "sssi_england.gpkg", "sssi_scotland.gpkg", "sssi_wales.gpkg",
                        "aonb_england.gpkg", "aonb_wales.gpkg",
                        "national_parks_england.gpkg", "national_parks_scotland.gpkg", "national_parks_wales.gpkg",
                        "ancient_woodland_england.gpkg", "ancient_woodland_scotland.gpkg", "ancient_woodland_wales.gpkg",
                        "defra_source_protection_zones.gpkg", "nrw_source_protection_zones.gpkg",
                        "sepa_dwpa_catchments.gpkg", "sepa_dwpa_groundwaters.gpkg",
                        "sepa_dwpa_lochs.gpkg", "sepa_dwpa_rivers.gpkg",
                        "uk_lcm_2024.tif"],
        "hazards": ["ea_flood_zones.gpkg", "sepa_flood_zones.gpkg", "sepa_flood_zones_surface.gpkg",
                    "sepa_flood_zones_coastal.gpkg", "nrw_flood_zones.gpkg", "nrw_flood_zones_surface.gpkg",
                    "airports_gb.gpkg", "ukceh_reservoirs.csv", "bgs_seismic_hazard.gpkg",
                    "nsta_onshore_wells.gpkg", "deep_geothermal.gpkg"],
        "marine": ["gebco_2025.tif", "marine_protected_scotland.gpkg"],
        "societal": ["output_areas_ew.gpkg", "output_areas_scotland.gpkg",
                    "green_belt_england.gpkg",
                    "alc_england_provisional.gpkg", "alc_wales_predictive.gpkg", "alc_scotland.gpkg"],
    }

    missing = []
    for subfolder, files in expected.items():
        for f in files:
            p = Path(land_data_path) / subfolder / f
            if not p.exists():
                missing.append(str(p))

    if missing:
        raise FileNotFoundError(
            "=" * 80 + "\n"
            "❌ MISSING LAND CONSTRAINT DATA FILES\n"
            ...
            "🔧 SOLUTION:\n"
            "Run data preparation:\n"
            "  python scripts/setup/prepare_raw_data.py --category land\n"
            "=" * 80
        )

# Fail-fast check
_validate_land_data_ready()
```

This keeps the validation pattern consistent with `renewables.smk` (helper function → fail-fast at DAG evaluation), while the actual extraction logic still lives in `scripts/setup/prepare_raw_data.py` as a standalone setup script. The `.smk` helper just _checks_ that setup was done — it doesn't _do_ the extraction.

The `prepare_raw_data.py` script is still worth having separately because it's called manually and can handle both land and water data categories. But the validation gate lives where your codebase expects it: as a helper function in the `.smk` file.
