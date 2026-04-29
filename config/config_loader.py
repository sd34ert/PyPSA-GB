"""
Configuration Loading Utility for PyPSA-GB

Handles the hierarchical configuration system:
  1. defaults.yaml    - Base defaults for all scenarios
  2. scenarios.yaml   - Scenario definitions (override defaults)
  3. config.yaml      - Run selection + global overrides
  4. clustering.yaml  - Clustering presets

Usage:
    from config_loader import load_config, get_scenario

    config = load_config()
    scenario = get_scenario("HT35", config)
"""

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_H2_EXPLICIT_DEMAND_PROFILES = {"flat", "timeseries", "hourly", "daily"}


def deep_merge(base: dict, override: dict) -> dict:
    """
    Deep merge two dictionaries. Override values take precedence.
    Nested dicts are merged recursively, not replaced entirely.
    """
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_yaml(filepath: Path) -> dict:
    """Load a YAML file, return empty dict if not found."""
    if not filepath.exists():
        return {}
    with open(filepath, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_clustering(clustering_value: Any, clustering_presets: dict) -> dict:
    """
    Resolve clustering configuration.

    Args:
        clustering_value: Either a string (preset name) or dict (inline config)
        clustering_presets: Dictionary of preset configurations

    Returns:
        Resolved clustering configuration dict

    Note:
        If a dict has 'method' specified, clustering is enabled regardless of
        the 'enabled' key (which may come from defaults). The 'enabled: false'
        only disables clustering if no method is specified.
    """
    if clustering_value is None:
        return {"enabled": False}

    if isinstance(clustering_value, str):
        # Reference to preset
        if clustering_value not in clustering_presets:
            raise ValueError(
                f"Unknown clustering preset: '{clustering_value}'. "
                f"Available: {list(clustering_presets.keys())}"
            )
        config = deepcopy(clustering_presets[clustering_value])
        config["enabled"] = True
        return config

    if isinstance(clustering_value, dict):
        # Allow referencing preset while extending with custom options
        if "preset" in clustering_value:
            preset_name = clustering_value.get("preset")
            if preset_name not in clustering_presets:
                raise ValueError(
                    f"Unknown clustering preset: '{preset_name}'. "
                    f"Available: {list(clustering_presets.keys())}"
                )
            config = deep_merge(
                deepcopy(clustering_presets[preset_name]),
                {k: v for k, v in clustering_value.items() if k != "preset"},
            )
            config["enabled"] = True
            return config

        # If 'method' is specified, clustering should be enabled
        # (even if 'enabled: false' was inherited from defaults)
        if "method" in clustering_value:
            config = deepcopy(clustering_value)
            config["enabled"] = True
            return config

        # No method specified - respect the enabled flag
        if not clustering_value.get("enabled", False):
            return {"enabled": False}

        # Enabled but no method - unusual, but keep the config
        config = deepcopy(clustering_value)
        config["enabled"] = True
        return config

    return {"enabled": False}


def load_config(config_dir: Path | None = None) -> dict[str, Any]:
    """
    Load the complete configuration with proper inheritance.

    Returns a dict with:
        - defaults: Default settings
        - scenarios: Dict of scenario_id -> merged scenario config
        - run_scenarios: List of scenario IDs to run
        - clustering_presets: Available clustering presets
        - global_overrides: Overrides from config.yaml
    """
    if config_dir is None:
        config_dir = Path(__file__).parent

    # Load all config files
    defaults = load_yaml(config_dir / "defaults.yaml")
    scenarios_raw = load_yaml(config_dir / "scenarios.yaml")
    main_config = load_yaml(config_dir / "config.yaml")
    clustering_config = load_yaml(config_dir / "clustering.yaml")

    # Support both old (scenarios_master.yaml) and new (scenarios.yaml) format
    if not scenarios_raw and (config_dir / "scenarios_master.yaml").exists():
        scenarios_master = load_yaml(config_dir / "scenarios_master.yaml")
        scenarios_raw = scenarios_master.get("scenarios", {})

    # Extract clustering presets
    clustering_presets = clustering_config.get("presets", {})
    aggregation_strategies = clustering_config.get("aggregation_strategies", {})

    # Extract global overrides from main config (excluding special keys)
    # Note: logging and network_naming are now in defaults.yaml, not config.yaml
    special_keys = {"run_scenarios", "solve_mode"}
    global_overrides = {k: v for k, v in main_config.items() if k not in special_keys}

    # Build merged scenarios
    merged_scenarios = {}
    for scenario_id, scenario_config in scenarios_raw.items():
        if scenario_config is None:
            continue

        # Skip template comments (strings that start with template markers)
        if isinstance(scenario_config, str):
            continue

        # Start with defaults
        merged = deepcopy(defaults)

        # Apply global overrides
        merged = deep_merge(merged, global_overrides)

        # Apply scenario-specific settings
        merged = deep_merge(merged, scenario_config)

        # Resolve clustering references
        if "clustering" in merged:
            merged["clustering"] = resolve_clustering(merged["clustering"], clustering_presets)

        # Add scenario ID for reference
        merged["_scenario_id"] = scenario_id

        merged_scenarios[scenario_id] = merged

    return {
        "defaults": defaults,
        "scenarios": merged_scenarios,
        "run_scenarios": main_config.get("run_scenarios", []),
        "clustering_presets": clustering_presets,
        "aggregation_strategies": aggregation_strategies,
        "global_overrides": global_overrides,
        "logging": defaults.get("logging", {}),
        "network_naming": defaults.get("network_naming", {}),
        "solve_mode": main_config.get("solve_mode", defaults.get("solve_mode", "LP")),
    }


def get_scenario(scenario_id: str, config: dict | None = None) -> dict[str, Any]:
    """
    Get a fully resolved scenario configuration.

    Args:
        scenario_id: The scenario identifier
        config: Pre-loaded config (optional, will load if not provided)

    Returns:
        Merged scenario configuration dict
    """
    if config is None:
        config = load_config()

    if scenario_id not in config["scenarios"]:
        available = list(config["scenarios"].keys())
        raise ValueError(f"Unknown scenario: '{scenario_id}'. Available: {available}")

    return config["scenarios"][scenario_id]


def get_active_scenarios(config: dict | None = None) -> list[dict[str, Any]]:
    """
    Get list of scenarios that are marked to run in config.yaml.

    Returns:
        List of merged scenario configuration dicts
    """
    if config is None:
        config = load_config()

    scenarios = []
    for scenario_id in config["run_scenarios"]:
        if scenario_id in config["scenarios"]:
            scenarios.append(config["scenarios"][scenario_id])
        else:
            print(f"Warning: Scenario '{scenario_id}' in run_scenarios not found")

    return scenarios


def _validate_positive(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate that a value is a positive number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return [f"[{scenario_id}] {field_name} must be a positive number, got: {value}"]
    return []


def _validate_non_negative(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate that a value is a non-negative number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return [f"[{scenario_id}] {field_name} must be a non-negative number, got: {value}"]
    return []


def _validate_fraction(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate that a value is a fraction in [0, 1]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1:
        return [f"[{scenario_id}] {field_name} must be a fraction in [0, 1], got: {value}"]
    return []


def _validate_boolean(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate that a value is a boolean."""
    if not isinstance(value, bool):
        return [f"[{scenario_id}] {field_name} must be true or false, got: {value}"]
    return []


def _validate_non_empty_string(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate that a value is a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        return [f"[{scenario_id}] {field_name} must be a non-empty string, got: {value}"]
    return []


def _validate_string_list(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate a non-empty list of non-empty strings."""
    if not isinstance(value, list) or len(value) == 0:
        return [f"[{scenario_id}] {field_name} must be a non-empty list"]
    if not all(isinstance(item, str) and item.strip() for item in value):
        return [f"[{scenario_id}] {field_name} must contain only non-empty strings"]
    return []


def _validate_optional_string_list(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate a list of non-empty strings, allowing an empty list."""
    if not isinstance(value, list):
        return [f"[{scenario_id}] {field_name} must be a list"]
    if not all(isinstance(item, str) and item.strip() for item in value):
        return [f"[{scenario_id}] {field_name} must contain only non-empty strings"]
    return []


def _validate_bool_map(
    mapping: Any,
    field_name: str,
    scenario_id: str,
    allowed_keys: tuple[str, ...] | None = None,
) -> list[str]:
    """Validate a dict of boolean flags."""
    errors = []
    if not isinstance(mapping, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping of boolean flags"]

    if allowed_keys is not None:
        unknown = sorted(set(mapping) - set(allowed_keys))
        if unknown:
            errors.append(
                f"[{scenario_id}] {field_name} contains unknown keys: {unknown}. "
                f"Allowed: {list(allowed_keys)}"
            )

    for key, value in mapping.items():
        errors.extend(_validate_boolean(value, f"{field_name}.{key}", scenario_id))
    return errors


def _validate_exclusion_zones(value: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate groundwater exclusion zones as int or list of ints."""
    if isinstance(value, int) and value in (1, 2, 3):
        return []
    if isinstance(value, list) and value and all(isinstance(zone, int) and zone in (1, 2, 3) for zone in value):
        return []
    return [
        f"[{scenario_id}] {field_name} must be 1, 2, 3 or a list containing only those values, got: {value}"
    ]


def _validate_land_cover_config(config: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate onshore land-cover exclusion settings."""
    errors = []
    if not isinstance(config, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping"]

    if "exclusion_codes" in config:
        codes = config["exclusion_codes"]
        if not isinstance(codes, list) or not all(isinstance(code, int) for code in codes):
            errors.append(
                f"[{scenario_id}] {field_name}.exclusion_codes must be a list of integers"
            )

    if "buffer_distances" in config:
        buffer_distances = config["buffer_distances"]
        if not isinstance(buffer_distances, dict):
            errors.append(
                f"[{scenario_id}] {field_name}.buffer_distances must be a mapping of code to distance"
            )
        else:
            for code, distance in buffer_distances.items():
                if not (isinstance(code, int) or (isinstance(code, str) and code.isdigit())):
                    errors.append(
                        f"[{scenario_id}] {field_name}.buffer_distances keys must be integers or digit strings, got: {code}"
                    )
                errors.extend(
                    _validate_non_negative(distance, f"{field_name}.buffer_distances.{code}", scenario_id)
                )

    return errors


def _validate_airfield_config(config: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate MoD/civil airfield switches."""
    if not isinstance(config, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping"]
    return _validate_bool_map(config, field_name, scenario_id, ("mod", "civil"))


def _validate_enabled_flag_config(config: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate a config block that only exposes an enabled flag."""
    if not isinstance(config, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping"]
    return _validate_bool_map(config, field_name, scenario_id, ("enabled",))


def _validate_buffered_feature_config(config: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate enabled/buffer config blocks."""
    errors = []
    if not isinstance(config, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping"]
    if "enabled" in config:
        errors.extend(_validate_boolean(config["enabled"], f"{field_name}.enabled", scenario_id))
    if "buffer" in config:
        errors.extend(_validate_non_negative(config["buffer"], f"{field_name}.buffer", scenario_id))
    return errors


def _validate_onshore_exclusion_config(config: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate shared onshore exclusion settings used by renewables and hydrogen."""
    errors = []
    if not isinstance(config, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping"]

    if "protected_tiers" in config:
        errors.extend(
            _validate_bool_map(
                config["protected_tiers"],
                f"{field_name}.protected_tiers",
                scenario_id,
                ("tier1", "tier2", "tier3", "tier4"),
            )
        )
    if "land_cover" in config:
        errors.extend(
            _validate_land_cover_config(
                config["land_cover"], f"{field_name}.land_cover", scenario_id
            )
        )
    if "flooding" in config:
        errors.extend(
            _validate_enabled_flag_config(config["flooding"], f"{field_name}.flooding", scenario_id)
        )
    if "coastal_change" in config:
        errors.extend(
            _validate_enabled_flag_config(
                config["coastal_change"], f"{field_name}.coastal_change", scenario_id
            )
        )
    if "groundwater_protection" in config:
        gw = config["groundwater_protection"]
        if not isinstance(gw, dict):
            errors.append(f"[{scenario_id}] {field_name}.groundwater_protection must be a mapping")
        elif "exclusion_zones" in gw:
            errors.extend(
                _validate_exclusion_zones(
                    gw["exclusion_zones"], f"{field_name}.groundwater_protection.exclusion_zones", scenario_id
                )
            )
    if "airfield" in config:
        errors.extend(
            _validate_airfield_config(config["airfield"], f"{field_name}.airfield", scenario_id)
        )
    for toggle_name in ("alc_bmv", "green_belt"):
        if toggle_name in config:
            errors.extend(
                _validate_enabled_flag_config(
                    config[toggle_name], f"{field_name}.{toggle_name}", scenario_id
                )
            )
    if "capacity_density" in config:
        errors.extend(
            _validate_positive(config["capacity_density"], f"{field_name}.capacity_density", scenario_id)
        )

    return errors


def _validate_paths_config(scenario: dict[str, Any]) -> list[str]:
    """Validate shared mutable path templates and referenced static inputs."""

    errors = []
    paths_cfg = scenario.get("paths", {})
    if not paths_cfg:
        return errors
    if not isinstance(paths_cfg, dict):
        sid = scenario.get("_scenario_id", "unknown")
        return [f"[{sid}] paths must be a mapping"]

    sid = scenario.get("_scenario_id", "unknown")
    repo_root = Path(__file__).resolve().parent.parent

    fes_cfg = paths_cfg.get("fes", {})
    if fes_cfg:
        if not isinstance(fes_cfg, dict):
            errors.append(f"[{sid}] paths.fes must be a mapping")
        else:
            for field in ("data_template", "processed_template"):
                if field in fes_cfg:
                    errors.extend(
                        _validate_non_empty_string(fes_cfg[field], f"paths.fes.{field}", sid)
                    )
                    if isinstance(fes_cfg[field], str) and "{year}" not in fes_cfg[field]:
                        errors.append(
                            f"[{sid}] paths.fes.{field} must contain '{{year}}' placeholder"
                        )

            if "api_urls_yaml" in fes_cfg:
                errors.extend(
                    _validate_non_empty_string(fes_cfg["api_urls_yaml"], "paths.fes.api_urls_yaml", sid)
                )
                if isinstance(fes_cfg["api_urls_yaml"], str):
                    api_urls_path = repo_root / fes_cfg["api_urls_yaml"]
                    if not api_urls_path.exists():
                        errors.append(
                            f"[{sid}] configured paths.fes.api_urls_yaml does not exist: {api_urls_path}"
                        )

    hydrogen_cfg = paths_cfg.get("hydrogen", {})
    if hydrogen_cfg:
        if not isinstance(hydrogen_cfg, dict):
            errors.append(f"[{sid}] paths.hydrogen must be a mapping")
        elif "demand_supply_distribution_workbook" in hydrogen_cfg:
            workbook_rel = hydrogen_cfg["demand_supply_distribution_workbook"]
            errors.extend(
                _validate_non_empty_string(
                workbook_rel,
                    "paths.hydrogen.demand_supply_distribution_workbook",
                    sid,
                )
            )
            if isinstance(workbook_rel, str):
                workbook_path = repo_root / workbook_rel
                if not workbook_path.exists():
                    errors.append(
                        f"[{sid}] configured "
                        "paths.hydrogen.demand_supply_distribution_workbook does not exist: "
                        f"{workbook_path}"
                    )

    return errors


def _validate_land_constraints(scenario: dict[str, Any]) -> list[str]:
    """Validate defaults-driven land_constraints schema."""
    errors = []
    lc = scenario.get("land_constraints", {})
    if not lc:
        return errors

    sid = scenario.get("_scenario_id", "unknown")

    if "enabled" in lc:
        errors.extend(_validate_boolean(lc["enabled"], "land_constraints.enabled", sid))

    # min_modelled_year
    if "min_modelled_year" in lc:
        if not isinstance(lc["min_modelled_year"], int) or lc["min_modelled_year"] <= 2024:
            errors.append(
                f"[{sid}] land_constraints.min_modelled_year must be int > 2024, "
                f"got: {lc['min_modelled_year']}"
            )

    # supported_network_models
    if "supported_network_models" in lc:
        errors.extend(
            _validate_string_list(
                lc["supported_network_models"],
                "land_constraints.supported_network_models",
                sid,
            )
        )

    # foundation
    foundation = lc.get("foundation", {})
    if foundation and not isinstance(foundation, dict):
        errors.append(f"[{sid}] land_constraints.foundation must be a mapping")
    if "resolution" in foundation:
        errors.extend(
            _validate_positive(
                foundation["resolution"], "land_constraints.foundation.resolution", sid
            )
        )
    for crs_key in ("target_crs", "source_crs"):
        if crs_key in foundation:
            val = foundation[crs_key]
            if not isinstance(val, int) or val <= 0:
                errors.append(
                    f"[{sid}] land_constraints.foundation.{crs_key} must be a positive integer EPSG code, got: {val}"
                )

    # onwind
    onwind = lc.get("onwind", {})
    if onwind:
        errors.extend(
            _validate_onshore_exclusion_config(onwind, "land_constraints.onwind", sid)
        )

    # Offshore wind config. Capacity density is active today; the siting keys
    # are reserved for future offshore filtering but still validated here.
    for carrier in ("offwind-ac", "offwind-dc", "offwind-float"):
        ow = lc.get(carrier, {})
        if ow and not isinstance(ow, dict):
            errors.append(f"[{sid}] land_constraints.{carrier} must be a mapping")
            continue
        if "min_depth" in ow:
            errors.extend(
                _validate_positive(ow["min_depth"], f"land_constraints.{carrier}.min_depth", sid)
            )
        if "max_depth" in ow:
            errors.extend(
                _validate_positive(ow["max_depth"], f"land_constraints.{carrier}.max_depth", sid)
            )
        if "min_shore_distance" in ow:
            errors.extend(
                _validate_non_negative(
                    ow["min_shore_distance"], f"land_constraints.{carrier}.min_shore_distance", sid
                )
            )
        if "max_shore_distance" in ow:
            errors.extend(
                _validate_non_negative(
                    ow["max_shore_distance"], f"land_constraints.{carrier}.max_shore_distance", sid
                )
            )
        if "min_depth" in ow and "max_depth" in ow and ow["max_depth"] < ow["min_depth"]:
            errors.append(
                f"[{sid}] land_constraints.{carrier}.max_depth must be >= min_depth"
            )
        if (
            "min_shore_distance" in ow
            and "max_shore_distance" in ow
            and ow["max_shore_distance"] < ow["min_shore_distance"]
        ):
            errors.append(
                f"[{sid}] land_constraints.{carrier}.max_shore_distance must be >= min_shore_distance"
            )
        if "kra_lease_area" in ow:
            errors.extend(
                _validate_boolean(ow["kra_lease_area"], f"land_constraints.{carrier}.kra_lease_area", sid)
            )
        if "capacity_density" in ow:
            density = ow["capacity_density"]
            if carrier == "offwind-float":
                if not isinstance(density, dict):
                    errors.append(
                        f"[{sid}] land_constraints.offwind-float.capacity_density must be a mapping with 'ac' and 'dc'"
                    )
                else:
                    for key in ("ac", "dc"):
                        if key not in density:
                            errors.append(
                                f"[{sid}] land_constraints.offwind-float.capacity_density missing '{key}'"
                            )
                        else:
                            errors.extend(
                                _validate_positive(
                                    density[key],
                                    f"land_constraints.offwind-float.capacity_density.{key}",
                                    sid,
                                )
                            )
            else:
                errors.extend(
                    _validate_positive(
                        density, f"land_constraints.{carrier}.capacity_density", sid
                    )
                )

    # solar
    solar = lc.get("solar", {})
    if solar:
        errors.extend(
            _validate_onshore_exclusion_config(solar, "land_constraints.solar", sid)
        )

    return errors


def _validate_nuclear_siting(scenario: dict[str, Any]) -> list[str]:
    """Validate defaults-driven nuclear siting schema."""
    errors = []
    nuclear_cfg = scenario.get("nuclear", {})
    nuc = nuclear_cfg.get("siting_constraints", {})
    if not nuclear_cfg:
        return errors

    sid = scenario.get("_scenario_id", "unknown")
    future_fixed = nuclear_cfg.get("future_fixed", {})
    if future_fixed:
        if not isinstance(future_fixed, dict):
            errors.append(f"[{sid}] nuclear.future_fixed must be a mapping")
        elif "exclude_scotland" in future_fixed:
            errors.extend(
                _validate_boolean(
                    future_fixed["exclude_scotland"],
                    "nuclear.future_fixed.exclude_scotland",
                    sid,
                )
            )

    existing_asset_overrides = nuclear_cfg.get("existing_asset_overrides", {})
    if existing_asset_overrides:
        if not isinstance(existing_asset_overrides, dict):
            errors.append(f"[{sid}] nuclear.existing_asset_overrides must be a mapping")
        elif "retain_sites" in existing_asset_overrides:
            errors.extend(
                _validate_optional_string_list(
                    existing_asset_overrides["retain_sites"],
                    "nuclear.existing_asset_overrides.retain_sites",
                    sid,
                )
            )

    if not nuc:
        return errors

    if "enabled" in nuc:
        errors.extend(_validate_boolean(nuc["enabled"], "nuclear.siting_constraints.enabled", sid))
    if "scotland_ban" in nuc:
        errors.extend(_validate_boolean(nuc["scotland_ban"], "nuclear.siting_constraints.scotland_ban", sid))

    # large_nuclear
    ln = nuc.get("large_nuclear", {})
    if ln and not isinstance(ln, dict):
        errors.append(f"[{sid}] nuclear.siting_constraints.large_nuclear must be a mapping")
    if "max_per_site_mw" in ln:
        errors.extend(
            _validate_positive(ln["max_per_site_mw"], "nuclear.large_nuclear.max_per_site_mw", sid)
        )
    if "en6_sites_only" in ln:
        errors.extend(
            _validate_boolean(ln["en6_sites_only"], "nuclear.large_nuclear.en6_sites_only", sid)
        )
    if "scotland_ban" in ln:
        errors.extend(
            _validate_boolean(
                ln["scotland_ban"],
                "nuclear.siting_constraints.large_nuclear.scotland_ban",
                sid,
            )
        )

    # smr
    smr = nuc.get("smr", {})
    if smr:
        if not isinstance(smr, dict):
            errors.append(f"[{sid}] nuclear.siting_constraints.smr must be a mapping")
        else:
            errors.extend(_validate_boolean(smr.get("enabled", False), "nuclear.smr.enabled", sid))
            if "scotland_ban" in smr:
                errors.extend(
                    _validate_boolean(
                        smr["scotland_ban"],
                        "nuclear.siting_constraints.smr.scotland_ban",
                        sid,
                    )
                )
            if "allowed_anchor_ids" in smr:
                errors.extend(
                    _validate_optional_string_list(
                        smr["allowed_anchor_ids"],
                        "nuclear.siting_constraints.smr.allowed_anchor_ids",
                        sid,
                    )
                )
            if "preserve_non_scottish_baseline" in smr:
                errors.extend(
                    _validate_boolean(
                        smr["preserve_non_scottish_baseline"],
                        "nuclear.siting_constraints.smr.preserve_non_scottish_baseline",
                        sid,
                    )
                )
            errors.extend(_validate_onshore_exclusion_config(smr, "nuclear.smr", sid))
            if "pop_density_criterion" in smr:
                errors.extend(
                    _validate_enabled_flag_config(
                        smr["pop_density_criterion"], "nuclear.smr.pop_density_criterion", sid
                    )
                )
            if "airfield_frz" in smr:
                errors.extend(
                    _validate_enabled_flag_config(
                        smr["airfield_frz"], "nuclear.smr.airfield_frz", sid
                    )
                )
            for buffered_key in ("gas_pipe", "comah"):
                if buffered_key in smr:
                    errors.extend(
                        _validate_buffered_feature_config(
                            smr[buffered_key], f"nuclear.smr.{buffered_key}", sid
                        )
                    )
            if "coastal_change_buffer" in smr:
                errors.extend(
                    _validate_non_negative(
                        smr["coastal_change_buffer"], "nuclear.smr.coastal_change_buffer", sid
                    )
                )
            if "flood_zones_enabled" in smr:
                errors.extend(
                    _validate_boolean(smr["flood_zones_enabled"], "nuclear.smr.flood_zones_enabled", sid)
                )
            if "water_constraint" in smr:
                errors.extend(
                    _validate_enabled_flag_config(
                        smr["water_constraint"], "nuclear.smr.water_constraint", sid
                    )
                )
            if "min_site_area" in smr:
                errors.extend(
                    _validate_positive(smr["min_site_area"], "nuclear.smr.min_site_area", sid)
                )
            if "zone_threshold" in smr:
                errors.extend(
                    _validate_fraction(smr["zone_threshold"], "nuclear.smr.zone_threshold", sid)
                )
            if "cooling_options" in smr:
                errors.extend(
                    _validate_string_list(smr["cooling_options"], "nuclear.smr.cooling_options", sid)
                )
            if "cooling_water_source" in smr:
                errors.extend(
                    _validate_string_list(
                        smr["cooling_water_source"], "nuclear.smr.cooling_water_source", sid
                    )
                )

    return errors


def _validate_hydrogen_siting(scenario: dict[str, Any]) -> list[str]:
    """Validate defaults-driven hydrogen siting schema."""
    errors = []
    h2 = scenario.get("hydrogen", {}).get("siting_constraints", {})
    if not h2:
        return errors

    sid = scenario.get("_scenario_id", "unknown")
    if "enabled" in h2:
        errors.extend(_validate_boolean(h2["enabled"], "hydrogen.siting_constraints.enabled", sid))

    # Validate both electrolysis and h2_turbine with same pattern
    for section_name in ("electrolysis", "h2_turbine"):
        section = h2.get(section_name, {})
        if not section:
            continue

        prefix = f"hydrogen.{section_name}"
        if not isinstance(section, dict):
            errors.append(f"[{sid}] {prefix} must be a mapping")
            continue

        errors.extend(_validate_onshore_exclusion_config(section, prefix, sid))
        for buffered_key in ("gas_pipe", "comah"):
            if buffered_key in section:
                errors.extend(
                    _validate_buffered_feature_config(
                        section[buffered_key], f"{prefix}.{buffered_key}", sid
                    )
                )
        if "min_site" in section:
            errors.extend(_validate_non_negative(section["min_site"], f"{prefix}.min_site", sid))

    return errors


def _validate_nuclear_technologies(scenario: dict[str, Any]) -> list[str]:
    """Validate nuclear-to-x metadata scaffolding for reactor classes."""
    errors = []
    sid = scenario.get("_scenario_id", "unknown")
    nuclear_tech = scenario.get("nuclear_technologies", {})
    if not nuclear_tech:
        return errors

    default_cfg = nuclear_tech.get("default", {})
    if default_cfg and not isinstance(default_cfg, dict):
        errors.append(f"[{sid}] nuclear_technologies.default must be a mapping")
    elif default_cfg:
        if "reactor_class" in default_cfg:
            errors.extend(
                _validate_non_empty_string(
                    default_cfg["reactor_class"],
                    "nuclear_technologies.default.reactor_class",
                    sid,
                )
            )
        for key in (
            "thermal_efficiency",
            "heat_extraction_max_low_fraction",
            "heat_extraction_max_high_fraction",
            "min_stable_fraction",
        ):
            if key in default_cfg:
                errors.extend(
                    _validate_fraction(
                        default_cfg[key], f"nuclear_technologies.default.{key}", sid
                    )
                )
        for key in ("power_loss_per_mwth_low", "power_loss_per_mwth_high"):
            if key in default_cfg:
                errors.extend(
                    _validate_non_negative(
                        default_cfg[key], f"nuclear_technologies.default.{key}", sid
                    )
                )

    carriers_cfg = nuclear_tech.get("carriers", {})
    if carriers_cfg and not isinstance(carriers_cfg, dict):
        errors.append(f"[{sid}] nuclear_technologies.carriers must be a mapping")
    elif carriers_cfg:
        for carrier_name, cfg in carriers_cfg.items():
            if not isinstance(cfg, dict):
                errors.append(
                    f"[{sid}] nuclear_technologies.carriers.{carrier_name} must be a mapping"
                )
                continue
            if "reactor_class" in cfg:
                errors.extend(
                    _validate_non_empty_string(
                        cfg["reactor_class"],
                        f"nuclear_technologies.carriers.{carrier_name}.reactor_class",
                        sid,
                    )
                )

    return errors


def _validate_explicit_h2_demand_layer(layer: Any, field_name: str, scenario_id: str) -> list[str]:
    """Validate one explicit hydrogen demand layer."""
    errors: list[str] = []
    if not isinstance(layer, dict):
        return [f"[{scenario_id}] {field_name} must be a mapping"]

    if "name" in layer:
        errors.extend(_validate_non_empty_string(layer["name"], f"{field_name}.name", scenario_id))
    if "annual_demand_twh" in layer:
        errors.extend(
            _validate_non_negative(
                layer["annual_demand_twh"],
                f"{field_name}.annual_demand_twh",
                scenario_id,
            )
        )
    if "nodes_path" in layer:
        errors.extend(
            _validate_non_empty_string(layer["nodes_path"], f"{field_name}.nodes_path", scenario_id)
        )
        if isinstance(layer["nodes_path"], str) and not Path(layer["nodes_path"]).exists():
            errors.append(
                f"[{scenario_id}] {field_name}.nodes_path does not exist: {layer['nodes_path']}"
            )
    if "profile" in layer:
        errors.extend(_validate_non_empty_string(layer["profile"], f"{field_name}.profile", scenario_id))
        if isinstance(layer["profile"], str) and layer["profile"] not in SUPPORTED_H2_EXPLICIT_DEMAND_PROFILES:
            errors.append(
                f"[{scenario_id}] {field_name}.profile must be one of {sorted(SUPPORTED_H2_EXPLICIT_DEMAND_PROFILES)}, got: {layer['profile']}"
            )
    if "profile_path" in layer:
        errors.extend(
            _validate_non_empty_string(layer["profile_path"], f"{field_name}.profile_path", scenario_id)
        )
        if isinstance(layer["profile_path"], str) and not Path(layer["profile_path"]).exists():
            errors.append(
                f"[{scenario_id}] {field_name}.profile_path does not exist: {layer['profile_path']}"
            )
    if "scale_profile_to_annual_demand" in layer:
        errors.extend(
            _validate_boolean(
                layer["scale_profile_to_annual_demand"],
                f"{field_name}.scale_profile_to_annual_demand",
                scenario_id,
            )
        )
    if "unmet_demand_penalty_gbp_per_mwh" in layer:
        errors.extend(
            _validate_non_negative(
                layer["unmet_demand_penalty_gbp_per_mwh"],
                f"{field_name}.unmet_demand_penalty_gbp_per_mwh",
                scenario_id,
            )
        )
    return errors


def _validate_hydrogen_network(scenario: dict[str, Any]) -> list[str]:
    """Validate hydrogen network mode and full-topology settings."""
    errors = []
    sid = scenario.get("_scenario_id", "unknown")
    hydrogen_cfg = scenario.get("hydrogen", {})
    network_cfg = hydrogen_cfg.get("network", {})

    mode = network_cfg.get("mode")
    if mode is not None:
        errors.extend(_validate_non_empty_string(mode, "hydrogen.network.mode", sid))
        if isinstance(mode, str) and mode not in {"full", "copperplate"}:
            errors.append(
                f"[{sid}] hydrogen.network.mode must be 'full' or 'copperplate', got: {mode}"
            )

    topology = network_cfg.get("topology", {})
    if mode == "full":
        if not isinstance(topology, dict):
            errors.append(f"[{sid}] hydrogen.network.topology must be a mapping")
        else:
            for field in ("buses_path", "links_path", "stores_path"):
                if field in topology:
                    errors.extend(
                        _validate_non_empty_string(
                            topology[field], f"hydrogen.network.topology.{field}", sid
                        )
                    )
                else:
                    errors.append(
                        f"[{sid}] hydrogen.network.topology.{field} is required when mode='full'"
                    )

    coupling = network_cfg.get("coupling", {})
    if coupling:
        if not isinstance(coupling, dict):
            errors.append(f"[{sid}] hydrogen.network.coupling must be a mapping")
        elif "method" in coupling:
            errors.extend(
                _validate_non_empty_string(
                    coupling["method"], "hydrogen.network.coupling.method", sid
                )
            )
            if isinstance(coupling["method"], str) and coupling["method"] != "nearest_h2_node":
                errors.append(
                    f"[{sid}] hydrogen.network.coupling.method must be 'nearest_h2_node' in v1, got: {coupling['method']}"
                )

    capacity_bounds = network_cfg.get("capacity_bounds", {})
    if capacity_bounds:
        if not isinstance(capacity_bounds, dict):
            errors.append(f"[{sid}] hydrogen.network.capacity_bounds must be a mapping")
        else:
            for field, allowed in {
                "electrolysis": {"fes_total"},
                "h2_turbine": {"fes_generation"},
            }.items():
                if field in capacity_bounds:
                    errors.extend(
                        _validate_non_empty_string(
                            capacity_bounds[field],
                            f"hydrogen.network.capacity_bounds.{field}",
                            sid,
                        )
                    )
                    if (
                        isinstance(capacity_bounds[field], str)
                        and capacity_bounds[field] not in allowed
                    ):
                        errors.append(
                            f"[{sid}] hydrogen.network.capacity_bounds.{field} must be one of {sorted(allowed)}, got: {capacity_bounds[field]}"
                        )
            if "storage_multiplier" in capacity_bounds:
                errors.extend(
                    _validate_positive(
                        capacity_bounds["storage_multiplier"],
                        "hydrogen.network.capacity_bounds.storage_multiplier",
                        sid,
                    )
                )
                if (
                    isinstance(capacity_bounds["storage_multiplier"], (int, float))
                    and capacity_bounds["storage_multiplier"] < 1.0
                ):
                    errors.append(
                        f"[{sid}] hydrogen.network.capacity_bounds.storage_multiplier must be >= 1.0"
                    )
            if "electrolysis_multiplier" in capacity_bounds:
                errors.extend(
                    _validate_non_negative(
                        capacity_bounds["electrolysis_multiplier"],
                        "hydrogen.network.capacity_bounds.electrolysis_multiplier",
                        sid,
                    )
                )

    pipeline_friction = network_cfg.get("pipeline_friction", {})
    if pipeline_friction:
        if not isinstance(pipeline_friction, dict):
            errors.append(f"[{sid}] hydrogen.network.pipeline_friction must be a mapping")
        else:
            for field in (
                "capacity_multiplier",
                "scotland_gb_capacity_multiplier",
                "marginal_cost_per_mwh_per_km",
                "efficiency_loss_per_100km",
                "min_efficiency",
            ):
                if field in pipeline_friction:
                    errors.extend(
                        _validate_non_negative(
                            pipeline_friction[field],
                            f"hydrogen.network.pipeline_friction.{field}",
                            sid,
                        )
                    )
            if "min_efficiency" in pipeline_friction:
                errors.extend(
                    _validate_fraction(
                        pipeline_friction["min_efficiency"],
                        "hydrogen.network.pipeline_friction.min_efficiency",
                        sid,
                    )
                )
            if "allow_pipeline_expansion" in pipeline_friction:
                errors.extend(
                    _validate_boolean(
                        pipeline_friction["allow_pipeline_expansion"],
                        "hydrogen.network.pipeline_friction.allow_pipeline_expansion",
                        sid,
                    )
                )
            if "capacity_group_multipliers" in pipeline_friction:
                multipliers = pipeline_friction["capacity_group_multipliers"]
                if not isinstance(multipliers, dict):
                    errors.append(
                        f"[{sid}] hydrogen.network.pipeline_friction.capacity_group_multipliers must be a mapping"
                    )
                else:
                    for group_name, multiplier in multipliers.items():
                        errors.extend(
                            _validate_non_negative(
                                multiplier,
                                f"hydrogen.network.pipeline_friction.capacity_group_multipliers.{group_name}",
                                sid,
                            )
                        )

    costs = network_cfg.get("costs", {})
    if costs:
        if not isinstance(costs, dict):
            errors.append(f"[{sid}] hydrogen.network.costs must be a mapping")
        else:
            for field in (
                "electrolysis_capital_cost",
                "h2_turbine_capital_cost",
                "storage_capital_cost",
                "scottish_tank_storage_capital_cost",
            ):
                if field in costs:
                    errors.extend(
                        _validate_non_negative(
                            costs[field], f"hydrogen.network.costs.{field}", sid
                        )
                    )

    explicit_demand = hydrogen_cfg.get("explicit_demand", {})
    if explicit_demand:
        if not isinstance(explicit_demand, dict):
            errors.append(f"[{sid}] hydrogen.explicit_demand must be a mapping")
        else:
            if "enabled" in explicit_demand:
                errors.extend(
                    _validate_boolean(
                        explicit_demand["enabled"],
                        "hydrogen.explicit_demand.enabled",
                        sid,
                    )
                )
            if "annual_demand_twh" in explicit_demand:
                errors.extend(
                    _validate_non_negative(
                        explicit_demand["annual_demand_twh"],
                        "hydrogen.explicit_demand.annual_demand_twh",
                        sid,
                    )
                )
            if "profile" in explicit_demand:
                errors.extend(
                    _validate_non_empty_string(
                        explicit_demand["profile"],
                        "hydrogen.explicit_demand.profile",
                        sid,
                    )
                )
                if (
                    isinstance(explicit_demand["profile"], str)
                    and explicit_demand["profile"] not in SUPPORTED_H2_EXPLICIT_DEMAND_PROFILES
                ):
                    errors.append(
                        f"[{sid}] hydrogen.explicit_demand.profile must be one of {sorted(SUPPORTED_H2_EXPLICIT_DEMAND_PROFILES)}, got: {explicit_demand['profile']}"
                    )
            if "nodes_path" in explicit_demand:
                errors.extend(
                    _validate_non_empty_string(
                        explicit_demand["nodes_path"],
                        "hydrogen.explicit_demand.nodes_path",
                        sid,
                    )
                )
                if isinstance(explicit_demand["nodes_path"], str) and not Path(
                    explicit_demand["nodes_path"]
                ).exists():
                    errors.append(
                        f"[{sid}] hydrogen.explicit_demand.nodes_path does not exist: {explicit_demand['nodes_path']}"
                    )
            if "unmet_demand_penalty_gbp_per_mwh" in explicit_demand:
                errors.extend(
                    _validate_non_negative(
                        explicit_demand["unmet_demand_penalty_gbp_per_mwh"],
                        "hydrogen.explicit_demand.unmet_demand_penalty_gbp_per_mwh",
                        sid,
                    )
                )
            if "layers" in explicit_demand:
                layers = explicit_demand["layers"]
                if not isinstance(layers, list):
                    errors.append(f"[{sid}] hydrogen.explicit_demand.layers must be a list")
                elif not layers:
                    errors.append(f"[{sid}] hydrogen.explicit_demand.layers must not be empty")
                else:
                    for idx, layer in enumerate(layers):
                        errors.extend(
                            _validate_explicit_h2_demand_layer(
                                layer,
                                f"hydrogen.explicit_demand.layers[{idx}]",
                                sid,
                            )
                        )

    return errors


def _validate_technical_potential_constraints(scenario: dict[str, Any]) -> list[str]:
    """Validate the per-scenario optimization switch for land-derived caps."""
    errors = []
    tpc = scenario.get("technical_potential_constraints", {})
    if not tpc:
        return errors

    sid = scenario.get("_scenario_id", "unknown")
    if "enabled" in tpc:
        errors.extend(
            _validate_boolean(
                tpc["enabled"], "technical_potential_constraints.enabled", sid
            )
        )
    if not tpc.get("enabled", False):
        return errors

    if "min_modelled_year" in tpc:
        if not isinstance(tpc["min_modelled_year"], int) or tpc["min_modelled_year"] <= 2024:
            errors.append(
                f"[{sid}] technical_potential_constraints.min_modelled_year must be int > 2024, got: {tpc['min_modelled_year']}"
            )
    if "supported_network_models" in tpc:
        errors.extend(
            _validate_string_list(
                tpc["supported_network_models"],
                "technical_potential_constraints.supported_network_models",
                sid,
            )
        )
    if "csv_path" in tpc:
        errors.extend(
            _validate_non_empty_string(
                tpc["csv_path"], "technical_potential_constraints.csv_path", sid
            )
        )

    modelled_year = scenario.get("modelled_year", 0)
    min_year = tpc.get("min_modelled_year", 2025)
    if modelled_year < min_year:
        errors.append(
            f"[{sid}] technical_potential_constraints enabled but modelled_year ({modelled_year}) is below minimum ({min_year})"
        )

    network_model = scenario.get("network_model", "ETYS")
    supported = tpc.get("supported_network_models", [])
    if supported and network_model.lower() not in [model.lower() for model in supported]:
        errors.append(
            f"[{sid}] technical_potential_constraints enabled but network_model '{network_model}' not in supported_network_models: {supported}"
        )

    lc = scenario.get("land_constraints", {})
    if not lc.get("enabled", False):
        errors.append(
            f"[{sid}] technical_potential_constraints enabled but land_constraints.enabled is false"
        )
    else:
        lc_supported = lc.get("supported_network_models", [])
        if lc_supported and network_model.lower() not in [model.lower() for model in lc_supported]:
            errors.append(
                f"[{sid}] technical_potential_constraints enabled but land_constraints.supported_network_models does not include '{network_model}'"
            )
        lc_min_year = lc.get("min_modelled_year", 2025)
        if modelled_year < lc_min_year:
            errors.append(
                f"[{sid}] technical_potential_constraints enabled but land_constraints.min_modelled_year ({lc_min_year}) exceeds modelled_year ({modelled_year})"
            )

    return errors


def _validate_future_capacity_candidates(scenario: dict[str, Any]) -> list[str]:
    """Validate future FES capacity candidate rollout config."""
    errors = []
    sid = scenario.get("_scenario_id", "unknown")
    cfg = scenario.get("future_capacity_candidates", {})
    if not cfg:
        return errors

    if "enabled" in cfg:
        errors.extend(
            _validate_boolean(cfg["enabled"], "future_capacity_candidates.enabled", sid)
        )
    if not cfg.get("enabled", False):
        return errors

    if "min_modelled_year" in cfg:
        if not isinstance(cfg["min_modelled_year"], int) or cfg["min_modelled_year"] <= 2024:
            errors.append(
                f"[{sid}] future_capacity_candidates.min_modelled_year must be int > 2024, got: {cfg['min_modelled_year']}"
            )
    if "supported_network_models" in cfg:
        errors.extend(
            _validate_string_list(
                cfg["supported_network_models"],
                "future_capacity_candidates.supported_network_models",
                sid,
            )
        )
    if "carriers" in cfg:
        errors.extend(
            _validate_string_list(
                cfg["carriers"], "future_capacity_candidates.carriers", sid
            )
        )
        allowed = {"wind_onshore", "wind_offshore", "solar_pv", "nuclear"}
        invalid = [
            carrier
            for carrier in cfg.get("carriers", [])
            if isinstance(carrier, str) and carrier not in allowed
        ]
        if invalid:
            errors.append(
                f"[{sid}] future_capacity_candidates.carriers contains unsupported carriers: {invalid}. Allowed: {sorted(allowed)}"
            )
    capital_costs = cfg.get("capital_costs", {})
    if capital_costs:
        if not isinstance(capital_costs, dict):
            errors.append(f"[{sid}] future_capacity_candidates.capital_costs must be a mapping")
        else:
            for carrier, value in capital_costs.items():
                errors.extend(
                    _validate_non_negative(
                        value,
                        f"future_capacity_candidates.capital_costs.{carrier}",
                        sid,
                    )
                )

    nuclear_cfg = cfg.get("nuclear", {})
    if nuclear_cfg and not isinstance(nuclear_cfg, dict):
        errors.append(f"[{sid}] future_capacity_candidates.nuclear must be a mapping")
        nuclear_cfg = {}

    if nuclear_cfg.get("enabled", False):
        workbook_paths = nuclear_cfg.get("workbook_paths", {})
        if not isinstance(workbook_paths, dict):
            errors.append(
                f"[{sid}] future_capacity_candidates.nuclear.workbook_paths must be a mapping"
            )
        else:
            fes_year = str(scenario.get("FES_year", ""))
            workbook_path = workbook_paths.get(fes_year)
            if not workbook_path:
                errors.append(
                    f"[{sid}] future_capacity_candidates.nuclear enabled but no workbook path configured for FES_year {fes_year}"
                )
            elif not Path(str(workbook_path)).exists():
                errors.append(
                    f"[{sid}] future_capacity_candidates.nuclear workbook path does not exist: {workbook_path}"
                )

        en6_sites_path = nuclear_cfg.get("en6_sites_path")
        if not isinstance(en6_sites_path, str) or not en6_sites_path.strip():
            errors.append(
                f"[{sid}] future_capacity_candidates.nuclear.en6_sites_path must be a non-empty string"
            )
        elif not Path(en6_sites_path).exists():
            errors.append(
                f"[{sid}] future_capacity_candidates.nuclear EN-6 site path does not exist: {en6_sites_path}"
            )

        smr_anchors_path = nuclear_cfg.get("smr_anchors_path")
        if not isinstance(smr_anchors_path, str) or not smr_anchors_path.strip():
            errors.append(
                f"[{sid}] future_capacity_candidates.nuclear.smr_anchors_path must be a non-empty string"
            )
        elif not Path(smr_anchors_path).exists():
            errors.append(
                f"[{sid}] future_capacity_candidates.nuclear SMR anchors path does not exist: {smr_anchors_path}"
            )

        smr_weights = nuclear_cfg.get("smr_demand_weights", {})
        if not isinstance(smr_weights, dict):
            errors.append(
                f"[{sid}] future_capacity_candidates.nuclear.smr_demand_weights must be a mapping"
            )
        else:
            for field in ("electricity", "gas"):
                if field not in smr_weights:
                    errors.append(
                        f"[{sid}] future_capacity_candidates.nuclear.smr_demand_weights missing required key: {field}"
                    )
                    continue
                errors.extend(
                    _validate_non_negative(
                        smr_weights[field],
                        f"future_capacity_candidates.nuclear.smr_demand_weights.{field}",
                        sid,
                    )
                )

    modelled_year = scenario.get("modelled_year", 0)
    min_year = cfg.get("min_modelled_year", 2025)
    if modelled_year < min_year:
        errors.append(
            f"[{sid}] future_capacity_candidates enabled but modelled_year ({modelled_year}) is below minimum ({min_year})"
        )

    network_model = scenario.get("network_model", "ETYS")
    supported = cfg.get("supported_network_models", [])
    if supported and network_model.lower() not in [model.lower() for model in supported]:
        errors.append(
            f"[{sid}] future_capacity_candidates enabled but network_model '{network_model}' not in supported_network_models: {supported}"
        )

    return errors


def validate_scenario(scenario: dict[str, Any]) -> list[str]:
    """
    Validate a scenario configuration.

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []
    scenario_id = scenario.get("_scenario_id", "unknown")

    # Required fields
    required = ["modelled_year", "renewables_year", "demand_year"]
    for field in required:
        if field not in scenario:
            errors.append(f"[{scenario_id}] Missing required field: {field}")

    # Future scenarios need FES config
    modelled_year = scenario.get("modelled_year", 0)
    if modelled_year > 2024:
        if "FES_year" not in scenario:
            errors.append(
                f"[{scenario_id}] Future scenario (year {modelled_year}) requires FES_year"
            )
        if "FES_scenario" not in scenario:
            errors.append(
                f"[{scenario_id}] Future scenario (year {modelled_year}) requires FES_scenario"
            )

    # Validate network_model
    valid_networks = ["ETYS", "Reduced", "Zonal"]
    network_model = scenario.get("network_model", "ETYS")
    if network_model not in valid_networks:
        errors.append(
            f"[{scenario_id}] Invalid network_model: {network_model}. Must be one of {valid_networks}"
        )

    # Validate defaults-driven land and siting schema plus scenario-scoped
    # technical potential activation.
    errors.extend(_validate_paths_config(scenario))
    errors.extend(_validate_land_constraints(scenario))
    errors.extend(_validate_nuclear_siting(scenario))
    errors.extend(_validate_nuclear_technologies(scenario))
    errors.extend(_validate_hydrogen_siting(scenario))
    errors.extend(_validate_hydrogen_network(scenario))
    errors.extend(_validate_technical_potential_constraints(scenario))
    errors.extend(_validate_future_capacity_candidates(scenario))

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# CLI for testing
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="PyPSA-GB Configuration Loader")
    parser.add_argument("--scenario", "-s", help="Show specific scenario config")
    parser.add_argument("--list", "-l", action="store_true", help="List all scenarios")
    parser.add_argument("--active", "-a", action="store_true", help="List active scenarios")
    parser.add_argument("--validate", "-v", action="store_true", help="Validate all scenarios")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    config = load_config()

    if args.list:
        print("Available scenarios:")
        for sid in sorted(config["scenarios"].keys()):
            desc = config["scenarios"][sid].get("description", "")
            print(f"  {sid}: {desc}")

    elif args.active:
        print("Active scenarios (from run_scenarios):")
        for sid in config["run_scenarios"]:
            if sid in config["scenarios"]:
                desc = config["scenarios"][sid].get("description", "")
                print(f"  [OK] {sid}: {desc}")
            else:
                print(f"  [ERROR] {sid}: NOT FOUND")

    elif args.validate:
        print("Validating scenarios...")
        all_errors = []
        for sid, scenario in config["scenarios"].items():
            errors = validate_scenario(scenario)
            all_errors.extend(errors)

        if all_errors:
            print(f"\n{len(all_errors)} validation errors:")
            for error in all_errors:
                print(f"  [ERROR] {error}")
        else:
            print(f"  [OK] All {len(config['scenarios'])} scenarios valid")

    elif args.scenario:
        scenario = get_scenario(args.scenario, config)
        if args.json:
            print(json.dumps(scenario, indent=2, default=str))
        else:
            print(f"Scenario: {args.scenario}")
            print("-" * 40)
            for key, value in sorted(scenario.items()):
                if not key.startswith("_"):
                    print(f"  {key}: {value}")

    else:
        parser.print_help()
