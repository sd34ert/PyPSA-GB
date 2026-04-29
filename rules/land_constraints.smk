"""
Land Use Constraints Rules for PyPSA-GB

Single rule file containing ALL GIS processing for the land constraints
pipeline. Every onshore generation technology follows a standardised
three-step pipeline: pixel-level exclusion → availability CSV →
technical potential (p_nom_max).

Architecture:
=============
Section A: SHARED FOUNDATION       — raw data → standardised rasters
Section B: RENEWABLE EXCLUSIONS    — foundation rasters → onshore/offshore availability
Section C: NUCLEAR & H₂ EXCLUSIONS — foundation + tech-specific → exclusion raster + availability
Section D: UNIFIED AGGREGATION     — merge per-tech CSVs → availability matrix → technical potential

Design Decisions:
=================
- No PyPSA network objects — outputs are rasters and CSVs only.
- Depends only on zone shapes from network_build.smk, so runs in parallel
  with demand.smk, renewables.smk, and the rest of the network pipeline.
- Zone-dependent rules use {network_model} wildcard (not {scenario}) so
  multiple scenarios sharing the same network model reuse the same outputs.
- Land constraint settings are read from global defaults.yaml and are shared
  across compatible scenarios. Scenario-level overrides are not supported in
  the active DAG.
- Each per-technology exclusion rule outputs BOTH an exclusion raster
  (for validation/QGIS/thesis maps) AND a per-zone availability CSV
  (for the unified pipeline).

DAG Parallelism:
================
                         ┌─ demand.smk ───────────┐
                         │                        │
network_build.smk ───────┼─ renewables.smk ───────┼──→ generators.smk → ...
                         │                        │
                         └─ land_constraints.smk ─┘

Constraint application is deferred to the post-assembly policy layer in
policy.smk, and the constrained network is then consumed by solve.smk.

Usage:
======
# Build technical potential for all technologies
snakemake resources/land/technical_potential_Zonal.csv --cores 4
"""

# =============================================================================
# PATH CONFIGURATION
# =============================================================================

resources_path = "resources"
data_path = "data"

# Access the global defaults-driven land policy from defaults.yaml. Scenario-
# level overrides are intentionally not wired into this workflow.
_defaults = _full_config["defaults"]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_zones_for_model(wildcards):
    """
    Get the zone shapes file for a given network_model.

    Currently only the Zonal network model is supported for land constraints.
    ETYS and Reduced network models do not use zone-based land constraints
    and should not trigger these rules.

    Parameters
    ----------
    wildcards : snakemake.io.Wildcards
        Must contain ``network_model`` attribute.

    Returns
    -------
    str
        Path to zone shapes file (GeoJSON).

    Raises
    ------
    ValueError
        If *network_model* is not "Zonal".
    """
    model = wildcards.network_model
    if model != "Zonal":
        raise ValueError(
            f"Land constraints are only supported for the Zonal network model, "
            f"got '{model}'. ETYS and Reduced models do not use zone-based "
            f"land constraints."
        )
    return f"{data_path}/network/zonal/zones.geojson"



# =============================================================================
# SECTION A: SHARED FOUNDATION
# =============================================================================
# Runs once. All technology branches consume these outputs.
# No wildcards on raster rules (GB-wide). Zone-dependent rules use
# {network_model} wildcard for deduplication across scenarios.
# =============================================================================


rule build_protected_areas_raster:
    """
    Merge all GB protected/environmental designation datasets into a
    standardised 4-band raster.

    Band 1: SAC + SPA + Ramsar + SSSI (excluding marine areas)
    Band 2: AONB + National Scenic Areas + National Parks
    Band 3: Irreplaceable habitats (ancient woodland, blanket bog,
            limestone pavement, coastal sand dunes, lowland fens)
    Band 4: Historic environment (WHS, scheduled monuments,
            registered parks & gardens, battlefields)

    Processing Steps:
    1. Read all 24 vector sources, reproject to EPSG:27700
    2. Merge by tier, dissolve overlapping geometries within each tier
    3. Rasterize each tier as a separate band at configured resolution
    4. Calculate zone-level fractions (% of each zone covered by each tier)
    5. Write 4-band GeoTIFF + zone fractions CSV

    Each technology enables/disables tiers via config (hard exclusion).

    Performance: ~5-10 min, 8 GB peak memory

    Transforms: 24 vector files → 4-band GeoTIFF + zone fractions CSV
    """
    input:
        # Tier 1: SAC/SPA/Ramsar/SSSI (excluding marine)
        sac=f"{data_path}/land/environment/gb_sac_excluding_marine.gpkg",
        spa=f"{data_path}/land/environment/gb_spa_excluding_marine.gpkg",
        ramsar=f"{data_path}/land/environment/gb_ramsar_excluding_marine.gpkg",
        sssi_eng=f"{data_path}/land/environment/sssi_england_excluding_marine.gpkg",
        sssi_sco=f"{data_path}/land/environment/sssi_scotland_excluding_marine.gpkg",
        sssi_wal=f"{data_path}/land/environment/sssi_wales_excluding_marine.gpkg",
        # Tier 2: AONB/NatParks/NSA
        aonb_eng=f"{data_path}/land/environment/aonb_england.gpkg",
        aonb_wal=f"{data_path}/land/environment/aonb_wales.gpkg",
        nsa_sco=f"{data_path}/land/environment/nsa_scotland.shp",
        natpark_eng=f"{data_path}/land/environment/national_parks_england.gpkg",
        natpark_wal=f"{data_path}/land/environment/national_parks_wales.gpkg",
        natpark_sco=f"{data_path}/land/environment/national_parks_scotland.gpkg",
        # Tier 3: Irreplaceable habitats
        aw_eng=f"{data_path}/land/environment/ancient_woodland_england.gpkg",
        aw_sco=f"{data_path}/land/environment/ancient_woodland_scotland.gpkg",
        aw_wal=f"{data_path}/land/environment/ancient_woodland_wales.gpkg",
        irr_eng=f"{data_path}/land/environment/irreplaceable_habitats_england.gpkg",
        irr_sco=f"{data_path}/land/environment/irreplaceable_habitats_scotland.gpkg",
        irr_bog_wal=f"{data_path}/land/environment/irreplaceable_habitats_blanket_bog_wales.gpkg",
        irr_dunes_wal=f"{data_path}/land/environment/irreplaceable_habitats_coastal_sand_dunes_wales.gpkg",
        irr_lime_wal=f"{data_path}/land/environment/irreplaceable_habitats_limestone_pavement_wales.gpkg",
        irr_fens_wal=f"{data_path}/land/environment/irreplaceable_habitats_lowland_fens_wales.gpkg",
        # Tier 4: Historic environment
        hist_eng=f"{data_path}/land/environment/historic_environment_england.gpkg",
        hist_sco=f"{data_path}/land/environment/historic_environment_scotland.gpkg",
        hist_wal=f"{data_path}/land/environment/historic_environment_wales.gpkg",
        zones=f"{data_path}/network/zonal/zones.geojson",
    output:
        raster=f"{resources_path}/land/protected_areas_gb.tif",
        zone_fractions=f"{resources_path}/land/protected_area_fractions.csv",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building protected areas raster (4-band: SAC/SPA/Ramsar/SSSI, AONB/NatParks, Irreplaceable Habitats, Historic Environment)"
    log:
        "logs/land/build_protected_areas_raster.log"
    benchmark:
        "benchmarks/land/build_protected_areas_raster.txt"
    resources:
        mem_mb=8000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_protected_areas_raster.py"


rule build_population_density_surface:
    """
    Create population density raster from Census 2021 Output Areas.

    Produces a continuous density surface (people/km²) at 100m resolution.
    Technology branches apply their own thresholds and buffer distances.

    Scotland GPKG has population counts embedded (Popcount, HHcount, sqkm).
    England & Wales GPKG contains boundaries only — population density
    (people/km²) is joined from a separate Census 2021 TS006 CSV at OA
    level (188,880 rows) via OA21CD code.

    Consumed by build_nuclear_pop_criterion (ONR demographic siting
    criterion). No separate zone CSV is needed here.
    """
    input:
        oa_shapes_ew=f"{data_path}/land/societal/output_areas_ew.gpkg",
        oa_density_ew=f"{data_path}/land/societal/output_areas_ew.csv",
        oa_shapes_sco=f"{data_path}/land/societal/output_areas_scotland.gpkg",
    output:
        raster=f"{resources_path}/land/population_density_gb.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building population density surface from Census 2021 Output Areas"
    log:
        "logs/land/build_population_density_surface.log"
    benchmark:
        "benchmarks/land/build_population_density_surface.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    resources:
        mem_mb=8000
    script:
        "../scripts/land/build_population_density_surface.py"


rule build_land_cover_raster:
    """
    Reproject and resample UK Land Cover Map 2024 to the canonical GB reference grid.

    The UK LCM 2024 (UKCEH) is a 25m raster with 21 habitat classes (codes 1–21).
    Reprojects to 100m using nearest-neighbour interpolation (preserving categorical
    values) and masks to Great Britain using dissolved GSP regions (removing NI).

    Processing Steps:
    1. Load source LCM raster and create canonical reference grid
    2. Reproject and resample from 25m to 100m (nearest-neighbour)
    3. Mask to Great Britain using dissolved GSP region boundaries
    4. Validate output class codes against expected LCM 2024 classes (1–21)
    5. Write single-band GeoTIFF (uint8, nodata=0)

    Transforms: 25m UK-wide LCM raster → 100m GB-only land cover GeoTIFF
    """
    input:
        lcm=f"{data_path}/land/societal/uk_lcm_2024.tif",
        gb_boundary=f"{data_path}/network/GSP/GSP_regions_20250109/GSP_regions_20250109.shp",
    output:
        raster=f"{resources_path}/land/land_cover_gb.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Reprojecting UK LCM 2024 to canonical GB reference grid (100m, 21 habitat classes)"
    log:
        "logs/land/build_land_cover_raster.log"
    benchmark:
        "benchmarks/land/build_land_cover_raster.txt"
    resources:
        mem_mb=6000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_land_cover_raster.py"



rule build_flooding_risk_raster:
    """
    Merge EA (England), NRW (Wales), and SEPA (Scotland) flooding data
    into unified risk raster. Includes river, surface water, and coastal
    flood maps from all three nations.
    Uses EA Flood Zones + Climate Change (England), EA Flood Zones (England)
    also available.
    """
    input:
        flood_eng=f"{data_path}/land/hazards/flood_zones_cc_england.gpkg",
        flood_sco_river=f"{data_path}/land/hazards/flood_zones_rivers_scotland.gpkg",
        flood_sco_surface=f"{data_path}/land/hazards/flood_zones_surface_scotland.gpkg",
        flood_sco_coastal=f"{data_path}/land/hazards/flood_zones_coastal_scotland.gpkg",
        flood_wal=f"{data_path}/land/hazards/flood_zones_seas_rivers_wales.gpkg",
        flood_wal_surface=f"{data_path}/land/hazards/flood_zones_surface_wales.gpkg",
    output:
        raster=f"{resources_path}/land/flooding_risk_gb.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building GB unified flooding risk raster"
    log:
        "logs/land/build_flooding_risk_raster.log"
    benchmark:
        "benchmarks/land/build_flooding_risk_raster.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    resources:
        mem_mb=6000
    script:
        "../scripts/land/build_flooding_risk_raster.py"


rule build_groundwater_protection:
    """
    Rasterize Source Protection Zones for England and Wales as a 3-band
    GeoTIFF (Band 1 = SPZ1, Band 2 = SPZ2, Band 3 = SPZ3). Scotland does
    not implement similar protection areas around drinking water sources
    and is excluded.

    Downstream consumers (per-technology exclusion rules) select bands
    via the per-technology exclusion_zones config parameter.
    """
    input:
        spz_eng=f"{data_path}/land/environment/source_protection_zones_england.gpkg",
        spz_wal=f"{data_path}/land/environment/source_protection_zones_wales.gpkg",
    output:
        raster=f"{resources_path}/land/groundwater_spz_ew.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building England & Wales 3-band groundwater protection raster"
    log:
        "logs/land/build_groundwater_protection.log"
    benchmark:
        "benchmarks/land/build_groundwater_protection.txt"
    resources:
        mem_mb=4000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_groundwater_protection.py"


rule build_airfield_raster:
    """
    Build 2-band FRZ exclusion raster from aerodrome Flight Restriction Zone data.

    Reads civil and military aerodrome FRZ CSVs (extracted from UK AIP ENR 5.1)
    with centre coordinates and FRZ circle radii. Buffers each point by its FRZ
    radius and rasterizes to a 2-band GeoTIFF.

    Output — airfields_gb.tif (2-band GeoTIFF, 100m, EPSG:27700):
        - Band 1: MoD/Military FRZ circles
        - Band 2: Civilian FRZ circles

    No downstream buffering needed — FRZ radius is baked into the raster.
    """
    input:
        civil_frz=f"{data_path}/land/hazards/civil_aerodromes_frz.csv",
        mod_frz=f"{data_path}/land/hazards/mod_aerodromes_frz.csv",
    output:
        raster=f"{resources_path}/land/airfields_gb.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building FRZ exclusion raster from aerodrome data"
    log:
        "logs/land/build_airfield_raster.log"
    benchmark:
        "benchmarks/land/build_airfield_raster.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    resources:
        mem_mb=4000
    script:
        "../scripts/land/build_airfield_raster.py"



rule build_green_belt_raster:
    """
    Rasterize Green Belt boundaries (England and Scotland) into a
    binary mask. Wales does not have statutory Green Belt.

    Green Belt is a soft constraint for solar and nuclear SMR siting.
    Applied as increased siting difficulty rather than absolute
    prohibition.

    Processing Steps:
    1. Load England Green Belt boundaries (DLUHC, gpkg)
    2. Load Scotland Green Belt boundaries (ScotGov, gpkg)
    3. Merge and dissolve overlapping geometries
    4. Rasterize as binary mask (1 = Green Belt, 0 = not)
    5. Write single-band GeoTIFF

    Transforms: 2 Green Belt GeoPackages → single-band binary GeoTIFF
    """
    input:
        gb_eng=f"{data_path}/land/societal/green_belt_england.gpkg",
        gb_sco=f"{data_path}/land/societal/green_belt_scotland.gpkg",
    output:
        raster=f"{resources_path}/land/green_belt_gb.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building Green Belt raster from England and Scotland boundaries"
    log:
        "logs/land/build_green_belt_raster.log"
    benchmark:
        "benchmarks/land/build_green_belt_raster.txt"
    resources:
        mem_mb=4000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_green_belt_raster.py"


rule build_alc_raster:
    """
    Load pre-filtered BMV agricultural land GeoPackages for England,
    Wales, and Scotland and rasterize into a unified binary mask.

    Input files contain only BMV features (Grades 1, 2, 3a for
    England/Wales; LCA classes 1, 2, 3.1 for Scotland). Used as
    hard constraint for solar and nuclear SMR siting — planning
    policy preference for lower-grade agricultural land.

    Processing Steps:
    1. Load 3 pre-filtered BMV GeoPackages
    2. Merge and dissolve overlapping geometries
    3. Rasterize as binary mask (1 = BMV, 0 = not BMV)
    4. Write single-band GeoTIFF

    Transforms: 3 BMV GeoPackages → single-band binary GeoTIFF
    """
    input:
        alc_eng=f"{data_path}/land/societal/alc_bmv_england.gpkg",
        alc_wal=f"{data_path}/land/societal/alc_bmv_wales.gpkg",
        alc_sco=f"{data_path}/land/societal/alc_bmv_scotland.gpkg",
    output:
        raster=f"{resources_path}/land/alc_bmv_gb.tif",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building BMV agricultural land raster from pre-filtered GeoPackages"
    log:
        "logs/land/build_alc_raster.log"
    benchmark:
        "benchmarks/land/build_alc_raster.txt"
    resources:
        mem_mb=4000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_alc_raster.py"


rule build_coastal_erosion_raster:
    """
    Build single-band coastal erosion exclusion raster from NCERM 2024 data.

    Loads 3 layers from the NCERM GeoPackage, rasterizes, and OR-merges:
    - NCERM_SMP_2105_70CC: SMP-delivered erosion at 2105 (70th %ile CC
      from UKCP18 RCP8.5). The 2105 horizon reflects long asset life of
      energy infrastructure, particularly nuclear (~60 years).
    - NCERM_Ground_Instability_Zone: historical ground instability areas
    - NCERM_Ground_Instability_Recession: predicted ground instability recession

    Transforms: NCERM 2024 GPKG (3 layers) → single-band binary GeoTIFF
    """
    input:
        coastal_erosion=f"{data_path}/land/hazards/coastal_erosion_uk_2024.gpkg",
    output:
        raster=f"{resources_path}/land/coastal_erosion_gb.tif",
    params:
        layers=lambda wc: _defaults.get('land_constraints', {}).get('coastal_change', {}).get('layers', [
            "NCERM_SMP_2105_70CC",
            "NCERM_Ground_Instability_Zone",
            "NCERM_Ground_Instability_Recession",
        ]),
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    message:
        "Building coastal erosion exclusion raster from NCERM 2024 data"
    log:
        "logs/land/build_coastal_erosion_raster.log"
    benchmark:
        "benchmarks/land/build_coastal_erosion_raster.txt"
    resources:
        mem_mb=2000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_coastal_erosion_raster.py"


rule build_scotland_mask:
    """
    Create binary raster mask for Scotland (nuclear exclusion).
    Simple but critical — nuclear is banned from Scotland.

    Uses {network_model} wildcard — zone shapes depend only on network_model.
    """
    input:
        zones=get_zones_for_model,
    output:
        mask=f"{resources_path}/land/scotland_mask_{{network_model}}.tif",
        scotland_zones=f"{resources_path}/land/scotland_zones_{{network_model}}.csv",
    params:
        # Scottish ESO zones — defined in defaults.yaml, merged into _full_config
        scottish_zones=lambda wc: _defaults.get('scotland', {}).get('zones', []),
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Building Scotland mask for {wildcards.network_model}"
    log:
        "logs/land/build_scotland_mask_{network_model}.log"
    benchmark:
        "benchmarks/land/build_scotland_mask_{network_model}.txt"
    resources:
        mem_mb=4000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_scotland_mask.py"


# =============================================================================
# SECTION B: RENEWABLE EXCLUSIONS
# =============================================================================
# Per-technology pixel-level exclusion for onshore renewables (onwind, solar).
# Offshore wind uses geometry-based KRA processing (not raster exclusion).
# Each rule outputs exclusion raster + per-zone availability CSV.
# =============================================================================


rule build_offshore_exclusions:
    """
    Build binary offshore exclusion raster from marine constraint layers.

    Rasterises all offshore exclusion zones (MPAs, shipping Q90, O&G fields,
    CCS licences, gas storage, tidal/wave plan options, marine mining)
    into a single binary raster: 1 = excluded, 0 = not excluded.

    This rule does NOT handle KRA processing or technology assignment —
    those are handled downstream by build_renewable_availability_matrix.
    """
    input:
        gb_eez_zones=f"{data_path}/land/marine/gb_eez.gpkg",
        marine_protected_gb=f"{data_path}/land/marine/offwind_protected_areas_gb.gpkg",
        shipping_density=f"{data_path}/land/marine/shipping_density_eu.geotiff",
        ccs_ew=f"{data_path}/land/marine/offshore_licensed_ccs_ew.gpkg",
        ccs_sco=f"{data_path}/land/marine/offshore_licensed_ccs_scotland.gpkg",
        gas_storage_gb=f"{data_path}/land/marine/offshore_gas_storage_sites_gb.gpkg",
        og_areas_gb=f"{data_path}/land/marine/offshore_o&g_zones_gb.gpkg",
        marine_mining_gb=f"{data_path}/land/marine/marine_mining_sites_gb.gpkg",
        marine_aggregates_gb=f"{data_path}/land/marine/marine_aggregates_sites_gb.gpkg",
        historic_environment_marine=f"{data_path}/land/marine/historic_environment_marine.gpkg",
        wave_sco=f"{data_path}/land/marine/wave_licensed_sites_scotland.gpkg",
        wave_ew=f"{data_path}/land/marine/wave_licensed_sites_ew.gpkg",
        tidal_sco=f"{data_path}/land/marine/tidal_licensed_sites_scotland.gpkg",
        tidal_ew=f"{data_path}/land/marine/tidal_licensed_sites_ew.gpkg",
    output:
        exclusion_raster=f"{resources_path}/land/offshore_exclusions.tif",
    params:
        config=lambda wc: _defaults.get('land_constraints', {}),
    message:
        "Building offshore exclusion raster from marine constraint layers"
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


rule build_offshore_available_kra:
    """
    Process offshore wind KRAs: subtract exclusions, classify TG/cost tiers,
    calculate distance-to-coast, intersect with zones, compute available area.

    Loads Fixed and Floating KRA polygons, subtracts the offshore exclusion
    raster, parses TG classification from Rating attribute, maps to cost tiers,
    calculates centroid distance to nearest coastline, classifies AC/DC
    connection type, and intersects with zone boundaries.

    Each output record is one KRA-zone intersection fragment with:
    kra_name, tg_class, cost_tier, capex_multiplier, kra_type, carrier,
    connection_type, distance_to_coast_km, zone_name, available_area_km2,
    centroid_x, centroid_y, geometry.
    """
    input:
        fixed_kra=f"{data_path}/land/marine/fixed_offwind_kra_gb.gpkg",
        floating_kra=f"{data_path}/land/marine/floating_offwind_kra_gb.gpkg",
        exclusion_raster=f"{resources_path}/land/offshore_exclusions.tif",
        zones=get_zones_for_model,
    output:
        available_kra=f"{resources_path}/land/offshore_available_kra_{{network_model}}.gpkg",
    params:
        config=lambda wc: _defaults.get('land_constraints', {}),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Processing offshore KRAs for {wildcards.network_model}: TG classification, exclusion subtraction, zone intersection"
    log:
        "logs/land/build_offshore_available_kra_{network_model}.log"
    benchmark:
        "benchmarks/land/build_offshore_available_kra_{network_model}.txt"
    resources:
        mem_mb=4000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_offshore_available_kra.py"


rule build_onshore_renewable_exclusions:
    """Build pixel-level exclusion rasters and per-zone availability for
    onshore renewable technologies (onwind, solar).

    Overlays 8 foundation exclusion rasters with technology-specific
    config from defaults.yaml. Each technology gets its own exclusion
    mask (different layers enabled, different buffer distances).

    Applies 8 exclusion layers per technology (each config-driven):
    1. Protected areas (4-band, per-tier enable/disable)
    2. FRZ exclusion zones (2-band: MoD/Civilian, per-band enable)
    3. Land cover (LCM 2024 codes, per-code buffers)
    4. Flooding risk (enable/disable)
    5. Groundwater SPZ (band selection per config)
    6. Coastal change (enable/disable)
    7. ALC BMV agricultural land (enable/disable)
    8. Green Belt (enable/disable)

    Outputs: multi-band exclusion raster (1 band per technology,
    0=available, 1=excluded) and per-zone availability CSV.

    Transforms: 8 foundation rasters + zone shapes → exclusion raster + CSV
    """
    input:
        protected=f"{resources_path}/land/protected_areas_gb.tif",
        airfields=f"{resources_path}/land/airfields_gb.tif",
        land_cover=f"{resources_path}/land/land_cover_gb.tif",
        flooding=f"{resources_path}/land/flooding_risk_gb.tif",
        groundwater=f"{resources_path}/land/groundwater_spz_ew.tif",
        coastal_erosion=f"{resources_path}/land/coastal_erosion_gb.tif",
        alc_bmv=f"{resources_path}/land/alc_bmv_gb.tif",
        green_belt=f"{resources_path}/land/green_belt_gb.tif",
        zones=get_zones_for_model,
    output:
        exclusion_raster=f"{resources_path}/land/onshore_renewable_exclusions_{{network_model}}.tif",
        availability_csv=f"{resources_path}/land/onshore_renewable_availability_{{network_model}}.csv",
    params:
        config=lambda wc: _defaults.get('land_constraints', {}),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Building onshore renewable exclusions and availability for {wildcards.network_model}"
    log:
        "logs/land/build_onshore_renewable_exclusions_{network_model}.log"
    benchmark:
        "benchmarks/land/build_onshore_renewable_exclusions_{network_model}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    threads: 4
    resources:
        mem_mb=12000
    script:
        "../scripts/land/build_onshore_renewable_exclusions.py"


# =============================================================================
# SECTION C: NUCLEAR & HYDROGEN EXCLUSIONS
# =============================================================================
# Per-technology pixel-level exclusion rules. Same pattern as onshore
# renewables: foundation rasters + tech-specific layers → exclusion raster
# + per-zone availability CSV.
# =============================================================================


rule build_nuclear_pop_criterion:
    """Apply ONR Semi-Urban demographic criterion for nuclear siting.

    Phase 1: all-around CWP via 30-band FFT convolution (SPF_360 < 1).
    Phase 2: 30-degree sector CWP for borderline candidates (SPF_theta < 1).
    Scotland excluded (nuclear ban). Output: binary ineligibility mask where
    pixels with SPF_MAX >= 1 are marked ineligible for SMR siting.
    """
    input:
        pop_density=f"{resources_path}/land/population_density_gb.tif",
        scotland_mask=f"{resources_path}/land/scotland_mask_{{network_model}}.tif",
        zones=get_zones_for_model,
    output:
        criterion=f"{resources_path}/land/nuclear_pop_criterion_{{network_model}}.tif",
        zone_fractions=f"{resources_path}/land/nuclear_pop_criterion_fractions_{{network_model}}.csv",
    params:
        resolution=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('resolution', 100),
        target_crs=lambda wc: _defaults.get('land_constraints', {}).get('foundation', {}).get('target_crs', 27700),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Building nuclear population density criterion for {wildcards.network_model}"
    log:
        "logs/land/build_nuclear_pop_criterion_{network_model}.log"
    benchmark:
        "benchmarks/land/build_nuclear_pop_criterion_{network_model}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    threads: 4
    script:
        "../scripts/land/nuclear_pop_density_criterion.py"


rule build_nuclear_smr_exclusions:
    """Build pixel-level SMR exclusion raster and per-zone availability.

    Combines 8 shared foundation exclusion layers with 5 nuclear-specific
    layers into a single binary exclusion raster, then calculates
    per-zone availability fractions (fraction of zone NOT excluded).

    Shared foundation layers (config-driven via nuclear.siting_constraints.smr):
    1. Protected areas (4-band, per-tier)
    2. Airfield FRZ (enabled via config)
    3. Land cover (codes 13,14,20,21)
    4. Flooding risk
    5. Groundwater SPZ (zones [1,2])
    6. Coastal erosion (with 3000m buffer)
    7. ALC BMV agricultural land
    8. Green Belt

    Nuclear-specific layers:
    9. Scotland ban (binary mask)
    10. ONR population criterion (binary raster)
    11. COMAH Upper Tier buffer (vector → rasterized)
    12. Gas pipeline buffer (vector → rasterized)
    13. Water availability (placeholder — disabled by default)

    Transforms: 10 rasters + 2 vector datasets + zone shapes
                → exclusion raster + availability CSV
    """
    input:
        protected=f"{resources_path}/land/protected_areas_gb.tif",
        airfields=f"{resources_path}/land/airfields_gb.tif",
        land_cover=f"{resources_path}/land/land_cover_gb.tif",
        flooding=f"{resources_path}/land/flooding_risk_gb.tif",
        groundwater=f"{resources_path}/land/groundwater_spz_ew.tif",
        coastal_erosion=f"{resources_path}/land/coastal_erosion_gb.tif",
        alc_bmv=f"{resources_path}/land/alc_bmv_gb.tif",
        green_belt=f"{resources_path}/land/green_belt_gb.tif",
        scotland_mask=f"{resources_path}/land/scotland_mask_{{network_model}}.tif",
        pop_criterion=f"{resources_path}/land/nuclear_pop_criterion_{{network_model}}.tif",
        comah=f"{data_path}/land/hazards/comah_upper-tier_ew_2025.csv",
        gas_pipe=f"{data_path}/land/hazards/Gas_Pipe.shp",
        zones=get_zones_for_model,
    output:
        exclusion_raster=f"{resources_path}/land/smr_exclusions_{{network_model}}.tif",
        availability_csv=f"{resources_path}/land/smr_availability_unfiltered_{{network_model}}.csv",
    params:
        nuclear_config=lambda wc: (
            _defaults.get('nuclear', {})
            .get('siting_constraints', {})
        ),
        lc_config=lambda wc: _defaults.get('land_constraints', {}),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Building SMR exclusion raster and availability for {wildcards.network_model}"
    log:
        "logs/land/build_nuclear_smr_exclusions_{network_model}.log"
    benchmark:
        "benchmarks/land/build_nuclear_smr_exclusions_{network_model}.txt"
    threads: 4
    resources:
        mem_mb=12000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_nuclear_smr_exclusions.py"


rule filter_smr_site_area:
    """Filter SMR availability by minimum contiguous site area and zone threshold.

    Reads the pixel-level exclusion raster from build_nuclear_smr_exclusions,
    inverts to get eligible pixels, labels contiguous regions (8-connectivity),
    removes regions smaller than min_site_area (default 1 km²), then
    calculates per-zone availability fractions from the filtered result.

    Zones with smr_available_frac below zone_threshold (default 0.01 = 1%)
    are set to zero — too little available land to be viable for SMR siting.

    Outputs both a filtered raster (for validation/reporting) and the
    per-zone availability CSV that feeds into build_availability_matrix.

    Transforms: exclusion raster + zone shapes → filtered raster + availability CSV
    """
    input:
        exclusion_raster=f"{resources_path}/land/smr_exclusions_{{network_model}}.tif",
        zones=get_zones_for_model,
    output:
        filtered_raster=f"{resources_path}/land/smr_site_filtered_{{network_model}}.tif",
        availability_csv=f"{resources_path}/land/smr_availability_{{network_model}}.csv",
    params:
        min_site_area=lambda wc: (
            _defaults.get('nuclear', {})
            .get('siting_constraints', {})
            .get('smr', {})
            .get('min_site_area', 1.0)
        ),
        resolution=lambda wc: (
            _defaults.get('land_constraints', {})
            .get('foundation', {})
            .get('resolution', 100)
        ),
        zone_threshold=lambda wc: (
            _defaults.get('nuclear', {})
            .get('siting_constraints', {})
            .get('smr', {})
            .get('zone_threshold', 0.01)
        ),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Filtering SMR availability by min site area ({wildcards.network_model})"
    log:
        "logs/land/filter_smr_site_area_{network_model}.log"
    benchmark:
        "benchmarks/land/filter_smr_site_area_{network_model}.txt"
    resources:
        mem_mb=12000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/filter_smr_site_area.py"


# =============================================================================
# SECTION D: UNIFIED AGGREGATION
# =============================================================================
# Merge per-technology availability CSVs into a single matrix, then
# calculate technical potential (p_nom_max) for all technologies.
# These are the final two rules in the land constraints pipeline.
# =============================================================================


rule build_availability_matrix:
    """Merge per-technology availability CSVs into a single unified matrix.

    Reads onshore renewable, nuclear SMR, and (future) hydrogen
    availability CSVs and combines them into one CSV with one row per
    zone and one column per technology.

    Simple tabular merge — no spatial processing.

    Transforms: per-tech availability CSVs → unified availability matrix
    """
    input:
        onshore=f"{resources_path}/land/onshore_renewable_availability_{{network_model}}.csv",
        smr=f"{resources_path}/land/smr_availability_{{network_model}}.csv",
    output:
        matrix=f"{resources_path}/land/availability_matrix_{{network_model}}.csv",
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Building unified availability matrix for {wildcards.network_model}"
    log:
        "logs/land/build_availability_matrix_{network_model}.log"
    benchmark:
        "benchmarks/land/build_availability_matrix_{network_model}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/build_availability_matrix.py"


rule calculate_technical_potential:
    """Convert availability fractions and available areas into technical
    potential (MW) per zone for ALL generation technologies.

    Onshore (onwind, solar, smr, electrolysis, h2_turbine):
        p_nom_max = availability_fraction × zone_area_km2 × capacity_density

    Offshore (offwind-fixed-ac/dc, offwind-float-ac/dc):
        p_nom_max = sum(available_area_km2) × capacity_density
        Aggregated by (zone, carrier, cost_tier) from KRA fragments.

    Output: single CSV with all onshore + offshore technologies.
    This is the gross technical potential used downstream as the
    scenario-configured p_nom_max input to the policy-layer constrained network step.

    Transforms: availability matrix + offshore KRA + zone areas
                → technical potential CSV
    """
    input:
        availability_matrix=f"{resources_path}/land/availability_matrix_{{network_model}}.csv",
        offshore_kra=f"{resources_path}/land/offshore_available_kra_{{network_model}}.gpkg",
    output:
        potential=f"{resources_path}/land/technical_potential_{{network_model}}.csv",
    params:
        config=lambda wc: _defaults.get('land_constraints', {}),
        nuclear_config=lambda wc: (
            _defaults.get('nuclear', {})
            .get('siting_constraints', {})
        ),
        hydrogen_config=lambda wc: (
            _defaults.get('hydrogen', {})
            .get('siting_constraints', {})
        ),
    wildcard_constraints:
        network_model="[^/]+"
    message:
        "Calculating technical potential for all technologies ({wildcards.network_model})"
    log:
        "logs/land/calculate_technical_potential_{network_model}.log"
    benchmark:
        "benchmarks/land/calculate_technical_potential_{network_model}.txt"
    resources:
        mem_mb=2000
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/land/calculate_technical_potential.py"
