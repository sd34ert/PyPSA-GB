"""
Scenario Detection and Auto-Configuration Module

This module handles intelligent detection of historical vs. future scenarios
and automatically configures data sources appropriately.

Key Functions:
- is_historical_scenario: Determine if scenario year is historical (≤2024)
- auto_configure_scenario: Automatically set data source based on scenario year
- validate_historical_scenario: Validate historical scenario configuration
- validate_future_scenario: Validate future scenario configuration
- validate_scenario_complete: Complete scenario validation
- summarize_scenario_configuration: Generate summary of scenario setup
"""

from pathlib import Path
from datetime import datetime
import logging
import os
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
if str(CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(CONFIG_DIR))

try:
    from config_loader import load_config as _load_full_config
    from config_loader import validate_scenario as _validate_merged_scenario
except Exception:
    _load_full_config = None
    _validate_merged_scenario = None

logger = logging.getLogger(__name__)


def is_historical_scenario(scenario_year):
    """
    Determine if a scenario year is historical (≤2024) or future (>2024).
    
    Args:
        scenario_year (int or dict): The year of the scenario, or scenario config dict
        
    Returns:
        bool: True if historical (≤2024), False if future (>2024)
    """
    # Handle both int and dict inputs
    if isinstance(scenario_year, dict):
        scenario_year = scenario_year.get('modelled_year') or scenario_year.get('year')
    
    if scenario_year is None:
        return False  # Treat None as future/unknown
    
    current_year = datetime.now().year
    threshold = min(2024, current_year)  # Dynamic threshold: never go beyond current year
    return scenario_year <= threshold


def auto_configure_scenario(scenario_dict):
    """
    Automatically configure scenario based on year (historical vs future).
    
    Args:
        scenario_dict (dict): The scenario configuration dictionary
        
    Returns:
        dict: Enhanced scenario configuration with auto-configured data sources
    """
    enhanced = dict(scenario_dict)
    # Try both 'modelled_year' and 'year' for compatibility
    scenario_year = scenario_dict.get("modelled_year") or scenario_dict.get("year", 2025)
    
    is_hist = is_historical_scenario(scenario_year)
    
    if is_hist:
        # Historical scenario: use DUKES + REPD
        enhanced["data_source"] = "historical"
        enhanced["generator_data_source"] = "DUKES"
        enhanced["demand_source"] = "ESPENI"
        enhanced["_needs_fes"] = False
    else:
        # Future scenario: use FES
        enhanced["data_source"] = "future"
        enhanced["generator_data_source"] = "FES"
        enhanced["demand_source"] = "FES"
        enhanced["_needs_fes"] = True
    
    return enhanced


def validate_historical_scenario(scenario_dict, scenario_id):
    """
    Validate a historical scenario configuration.
    
    Args:
        scenario_dict (dict): The scenario configuration
        scenario_id (str): The scenario identifier
        
    Returns:
        tuple: (is_valid, errors, warnings)
    """
    errors = []
    warnings = []
    
    # Check year (try both field names)
    year = scenario_dict.get("modelled_year") or scenario_dict.get("year")
    if not year:
        errors.append(f"Scenario {scenario_id}: Missing 'modelled_year' or 'year' field")
    elif year > 2024:
        errors.append(f"Scenario {scenario_id}: Year {year} > 2024, should be future scenario")
    
    # Check network model
    if "network_model" not in scenario_dict:
        errors.append(f"Scenario {scenario_id}: Missing 'network_model'")
    
    # Warnings for old data
    if year and year < 2020:
        warnings.append(f"Scenario {scenario_id}: Using data from year {year} (>4 years old)")
    
    return (len(errors) == 0, errors, warnings)


def validate_future_scenario(scenario_dict, scenario_id):
    """
    Validate a future scenario configuration.
    
    Args:
        scenario_dict (dict): The scenario configuration
        scenario_id (str): The scenario identifier
        
    Returns:
        tuple: (is_valid, errors, warnings)
    """
    errors = []
    warnings = []
    
    # Check year (try both field names)
    year = scenario_dict.get("modelled_year") or scenario_dict.get("year")
    if not year:
        errors.append(f"Scenario {scenario_id}: Missing 'modelled_year' or 'year' field")
    elif year <= 2024:
        errors.append(f"Scenario {scenario_id}: Year {year} ≤ 2024, should be historical scenario")
    
    # Check network model
    if "network_model" not in scenario_dict:
        errors.append(f"Scenario {scenario_id}: Missing 'network_model'")
    
    # Check FES scenario
    if "FES_scenario" not in scenario_dict:
        warnings.append(f"Scenario {scenario_id}: No FES scenario specified")
    
    return (len(errors) == 0, errors, warnings)


def validate_scenario_complete(scenario_dict):
    """
    Perform complete validation of a scenario.
    
    Args:
        scenario_dict (dict): The scenario configuration
        
    Returns:
        dict: Validation result with 'errors', 'warnings', 'info' keys
    """
    errors = []
    warnings = []
    info = []
    scenario_id = scenario_dict.get("_scenario_id", "unknown")
    
    # Get year (try both field names)
    year = scenario_dict.get("modelled_year") or scenario_dict.get("year")

    # Use config_loader.py as the single source of truth for hard validation.
    if _validate_merged_scenario is not None:
        errors.extend(_validate_merged_scenario(scenario_dict))
    else:
        # Fallback for unusual standalone contexts where config_loader import fails.
        if is_historical_scenario(year):
            _, hist_errors, hist_warnings = validate_historical_scenario(scenario_dict, scenario_id)
            errors.extend(hist_errors)
            warnings.extend(hist_warnings)
        else:
            _, fut_errors, fut_warnings = validate_future_scenario(scenario_dict, scenario_id)
            errors.extend(fut_errors)
            warnings.extend(fut_warnings)

        if "network_model" in scenario_dict:
            model = scenario_dict["network_model"]
            if model not in ["ETYS", "Reduced", "Zonal"]:
                errors.append(f"Invalid network_model '{model}'")

    # Keep lightweight warnings/info in this module.
    if is_historical_scenario(year):
        _, _, hist_warnings = validate_historical_scenario(scenario_dict, scenario_id)
        warnings.extend(hist_warnings)
    else:
        _, _, fut_warnings = validate_future_scenario(scenario_dict, scenario_id)
        warnings.extend(fut_warnings)
    
    return {
        "errors": errors,
        "warnings": warnings,
        "info": info
    }


def summarize_scenario_configuration(scenarios_dict_or_dict):
    """
    Generate a summary of scenario configuration(s).
    
    Args:
        scenarios_dict_or_dict: Either a dict of {scenario_id: scenario_config} or a single scenario config
        
    Returns:
        dict: Summary with 'historical', 'future', 'dukes_years_needed', 'fes_years_needed', 'cutout_years_needed'
    """
    # Handle both dict of scenarios and single scenario
    if not isinstance(scenarios_dict_or_dict, dict):
        return {}
    
    # Check if this is a dict of scenarios or a single scenario
    first_val = next(iter(scenarios_dict_or_dict.values()), None)
    
    if first_val and isinstance(first_val, dict) and "network_model" in first_val:
        # This is {scenario_id: scenario_config}
        scenarios_to_summarize = scenarios_dict_or_dict
    else:
        # Single scenario config
        return {
            "historical": [],
            "future": [],
            "dukes_years_needed": set(),
            "fes_years_needed": set(),
            "cutout_years_needed": set()
        }
    
    historical = []
    future = []
    dukes_years = set()
    fes_years = set()
    cutout_years = set()
    
    for scenario_id, config in scenarios_to_summarize.items():
        year = config.get("modelled_year") or config.get("year")
        
        if is_historical_scenario(year):
            historical.append(scenario_id)
            if year:
                dukes_years.add(year)
                cutout_years.add(year)
        else:
            future.append(scenario_id)
            # Future scenarios use renewables_year for weather cutouts
            renewables_year = config.get("renewables_year")
            if renewables_year:
                cutout_years.add(renewables_year)
            fes_year = config.get("FES_year", 2025)
            if fes_year:
                fes_years.add(fes_year)
    
    return {
        "historical": historical,
        "future": future,
        "dukes_years_needed": list(sorted(dukes_years)),
        "fes_years_needed": list(sorted(fes_years)),
        "cutout_years_needed": list(sorted(cutout_years))
    }


def check_cutout_availability(year):
    """
    Check if a weather cutout (Atlite cutout) is available for a given year.
    
    Args:
        year (int): The year to check
        
    Returns:
        bool: True if cutout exists, False otherwise
    """
    # Default cutout location
    cutout_path = Path("resources/atlite") / f"uk-{year}.nc"
    
    if cutout_path.exists():
        return True
    
    # Alternative location
    alt_cutout_path = Path("data/atlite") / f"uk-{year}.nc"
    
    return alt_cutout_path.exists()


def validate_all_active_scenarios(config_path, scenarios_path, check_files=True, verbose=True):
    """
    Validate all active scenarios defined in config.yaml.
    
    Args:
        config_path (Path): Path to config.yaml
        scenarios_path (Path): Path to scenarios.yaml
        check_files (bool): Whether to check for file existence
        verbose (bool): Whether to print detailed output
        
    Returns:
        dict: Validation result with 'valid' (bool) and 'summary' (dict)
    """
    if _load_full_config is None:
        if verbose:
            print("Error loading config files: config_loader import unavailable")
        return {'valid': False, 'summary': {'total_errors': 1}}

    try:
        config_dir = Path(config_path).parent
        merged_config = _load_full_config(config_dir)
    except Exception as e:
        if verbose:
            print(f"Error loading config files: {e}")
        return {'valid': False, 'summary': {'total_errors': 1}}

    active_scenarios = merged_config.get('run_scenarios', [])
    scenarios_def = merged_config.get("scenarios", {})
    
    total_errors = 0
    total_warnings = 0
    
    if verbose:
        print(f"Validating {len(active_scenarios)} active scenarios...")
        
    for scenario_id in active_scenarios:
        if scenario_id not in scenarios_def:
            if verbose:
                print(f"ERROR: Scenario '{scenario_id}' listed in config.yaml but not found in scenarios.yaml")
            total_errors += 1
            continue
            
        scenario_config = scenarios_def[scenario_id]
        result = validate_scenario_complete(scenario_config)
        
        errors = result['errors']
        warnings = result['warnings']
        
        if errors:
            total_errors += len(errors)
            if verbose:
                print(f"[ERROR] Scenario '{scenario_id}': {len(errors)} errors")
                for err in errors:
                    print(f"  - {err}")
        elif verbose:
            print(f"[OK] Scenario '{scenario_id}': Valid")
            
        if warnings:
            total_warnings += len(warnings)
            if verbose:
                for warn in warnings:
                    print(f"  - Warning: {warn}")
                    
        # File checks (basic implementation)
        if check_files:
            # Check cutout
            cutout_year = (
                scenario_config.get("modelled_year")
                if is_historical_scenario(scenario_config)
                else scenario_config.get("renewables_year")
            )
            if cutout_year and not check_cutout_availability(cutout_year):
                if verbose:
                    print(f"  - ERROR: Weather cutout for year {cutout_year} not found")
                total_errors += 1

    return {
        'valid': total_errors == 0,
        'summary': {
            'total_errors': total_errors,
            'total_warnings': total_warnings
        }
    }

