#!/usr/bin/env python3
"""
Add nuclear metadata scaffolding to an already integrated network.

This keeps the current reactor representation as generators, while attaching
metadata needed for later nuclear-to-hydrogen and heat-extraction work.
"""

import logging
import warnings

from scripts.utilities.network_io import load_network, save_network

warnings.filterwarnings("ignore", message="The network has not been optimized yet")

try:
    from scripts.utilities.logging_config import setup_logging, get_snakemake_logger

    if "snakemake" in globals():
        logger = get_snakemake_logger()
    else:
        logger = setup_logging("add_nuclear_metadata")
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    logger = logging.getLogger(__name__)


DEFAULT_NUCLEAR_CARRIERS = ("nuclear", "PWR", "AGR", "BWR", "AMR", "SMR", "smr")


def _apply_nuclear_metadata(network, scenario_config):
    nuclear_cfg = scenario_config.get("nuclear_technologies", {})
    default_cfg = nuclear_cfg.get("default", {}) if isinstance(nuclear_cfg, dict) else {}
    carriers_cfg = nuclear_cfg.get("carriers", {}) if isinstance(nuclear_cfg, dict) else {}

    configured_carriers = tuple(carriers_cfg.keys()) if isinstance(carriers_cfg, dict) else ()
    target_carriers = configured_carriers or DEFAULT_NUCLEAR_CARRIERS

    if len(network.generators) == 0:
        logger.info("No generators present - skipping nuclear metadata stage")
        return network

    mask = network.generators["carrier"].astype(str).isin(target_carriers)
    if not mask.any():
        logger.info("No nuclear generators found - passing network through unchanged")
        return network

    metadata_columns = {
        "reactor_class": "",
        "nuclear_thermal_efficiency": None,
        "heat_extraction_max_low_fraction": None,
        "heat_extraction_max_high_fraction": None,
        "power_loss_per_mwth_low": None,
        "power_loss_per_mwth_high": None,
        "nuclear_min_stable_fraction": None,
        "supports_low_grade_heat": False,
        "supports_high_grade_heat": False,
    }
    for column, default_value in metadata_columns.items():
        if column not in network.generators.columns:
            network.generators[column] = default_value

    for gen_name in network.generators.index[mask]:
        carrier = str(network.generators.at[gen_name, "carrier"])
        carrier_cfg = carriers_cfg.get(carrier, {}) if isinstance(carriers_cfg, dict) else {}
        merged_cfg = {**default_cfg, **carrier_cfg}

        network.generators.at[gen_name, "reactor_class"] = merged_cfg.get("reactor_class", carrier)
        network.generators.at[gen_name, "nuclear_thermal_efficiency"] = merged_cfg.get(
            "thermal_efficiency"
        )
        network.generators.at[gen_name, "heat_extraction_max_low_fraction"] = merged_cfg.get(
            "heat_extraction_max_low_fraction"
        )
        network.generators.at[gen_name, "heat_extraction_max_high_fraction"] = merged_cfg.get(
            "heat_extraction_max_high_fraction"
        )
        network.generators.at[gen_name, "power_loss_per_mwth_low"] = merged_cfg.get(
            "power_loss_per_mwth_low"
        )
        network.generators.at[gen_name, "power_loss_per_mwth_high"] = merged_cfg.get(
            "power_loss_per_mwth_high"
        )
        network.generators.at[gen_name, "nuclear_min_stable_fraction"] = merged_cfg.get(
            "min_stable_fraction"
        )
        network.generators.at[gen_name, "supports_low_grade_heat"] = True
        network.generators.at[gen_name, "supports_high_grade_heat"] = True

    if not hasattr(network, "meta") or network.meta is None:
        network.meta = {}
    network.meta["nuclear_metadata_scaffolded"] = True

    logger.info("Annotated %s nuclear generators", int(mask.sum()))
    logger.info(
        "Nuclear carriers present: %s",
        sorted(network.generators.loc[mask, "carrier"].astype(str).unique().tolist()),
    )
    return network


def main():
    if "snakemake" not in globals():
        raise RuntimeError("This script is intended to run under Snakemake")

    input_path = snakemake.input.network
    output_path = snakemake.output.network
    scenario_config = snakemake.params.scenario_config
    scenario = snakemake.params.scenario

    logger.info("Adding nuclear metadata for scenario: %s", scenario)
    network = load_network(input_path, custom_logger=logger)
    network = _apply_nuclear_metadata(network, scenario_config)
    save_network(network, output_path)
    logger.info("Saved nuclear-annotated network to: %s", output_path)


if __name__ == "__main__":
    main()
