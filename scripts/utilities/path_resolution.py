"""
Shared path-resolution helpers for repo-relative inputs.

This module centralizes mutable input paths that would otherwise be repeated
across Snakemake rules, scripts, and helper utilities.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = PROJECT_ROOT / "config" / "defaults.yaml"

DEFAULT_PATHS_CONFIG = {
    "fes": {
        "data_template": "resources/FES/FES_{year}_data.csv",
        "processed_template": "resources/FES/FES_{year}_processed.csv",
        "api_urls_yaml": "data/FES/FES_api_urls.yaml",
    },
    "hydrogen": {
        "demand_supply_distribution_workbook": "data/hydrogen/demand_supply_distribution.xlsx",
    },
}


def get_project_root() -> Path:
    """Return the repository root."""

    return PROJECT_ROOT


def resolve_repo_path(path_like: str | Path, project_root: Path | None = None) -> Path:
    """Resolve a repo-relative path to an absolute path."""

    root = project_root or PROJECT_ROOT
    path = Path(path_like)
    return path if path.is_absolute() else root / path


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dictionaries."""

    merged = dict(base)
    for key, value in override.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=1)
def load_paths_config(defaults_path: Path | None = None) -> dict[str, Any]:
    """Load the shared paths config from defaults.yaml, merged with safe defaults."""

    config_path = defaults_path or DEFAULTS_PATH
    config_data: dict[str, Any] = {}

    if Path(config_path).exists():
        with open(config_path, encoding="utf-8") as handle:
            config_data = yaml.safe_load(handle) or {}

    paths_cfg = config_data.get("paths", {})
    if not isinstance(paths_cfg, dict):
        paths_cfg = {}

    return _deep_merge(DEFAULT_PATHS_CONFIG, paths_cfg)


def _get_template(section: str, key: str) -> str:
    """Fetch a configured template/value from the shared paths config."""

    paths_cfg = load_paths_config()
    value = paths_cfg.get(section, {}).get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing configured path template for paths.{section}.{key}")
    return value


def get_fes_data_path(fes_year: int | str) -> Path:
    """Return the repo-relative FES data path for a given FES release year."""

    template = _get_template("fes", "data_template")
    return resolve_repo_path(template.format(year=fes_year))


def get_fes_processed_path(fes_year: int | str) -> Path:
    """Return the repo-relative processed FES path for a given FES release year."""

    template = _get_template("fes", "processed_template")
    return resolve_repo_path(template.format(year=fes_year))


def get_fes_api_urls_path() -> Path:
    """Return the configured FES API URL manifest path."""

    return resolve_repo_path(_get_template("fes", "api_urls_yaml"))


def get_hydrogen_demand_supply_distribution_workbook_path() -> Path:
    """Return the canonical hydrogen demand/supply distribution workbook path."""

    return resolve_repo_path(_get_template("hydrogen", "demand_supply_distribution_workbook"))
