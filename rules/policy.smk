"""
Policy-layer network mutations applied after the full network is assembled.

This stage is the authoritative producer of {scenario}_network_constrained.nc.
It currently hosts technical potential constraints so constrained runs preserve
storage, hydrogen, interconnectors, and marginal costs.
"""

resources_path = "resources"


def _should_apply_technical_potential_constraints_policy(scenario_config):
    constraints_config = scenario_config.get("technical_potential_constraints", {})
    if not constraints_config.get("enabled", False):
        return False

    min_year = constraints_config.get("min_modelled_year", 2025)
    if scenario_config.get("modelled_year", 2020) < min_year:
        return False

    supported = constraints_config.get("supported_network_models", ["zonal"])
    network_model = scenario_config.get("network_model", "Zonal").lower()
    return network_model in [model.lower() for model in supported]


def _get_technical_potential_csv_path_policy(scenario_config):
    return scenario_config.get("technical_potential_constraints", {}).get(
        "csv_path", f"{resources_path}/land/technical_potential_Zonal.csv"
    )


rule apply_technical_potential_constraints:
    """
    Apply technical-potential caps to the fully assembled pre-solve network.

    Transforms:
      {scenario}_network_demand_renewables_thermal_generators_storage_nuclear_hydrogen_interconnectors.nc
      -> {scenario}_network_constrained.nc
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_network_demand_renewables_thermal_generators_storage_nuclear_hydrogen_interconnectors.nc",
        technical_potential=lambda wc: (
            _get_technical_potential_csv_path_policy(scenarios[wc.scenario])
            if _should_apply_technical_potential_constraints_policy(scenarios[wc.scenario])
            else []
        )
    output:
        network=f"{resources_path}/network/{{scenario}}_network_constrained.nc",
        report=f"{resources_path}/generators/{{scenario}}_constrained_report.txt"
    params:
        apply_constraints=lambda wc: _should_apply_technical_potential_constraints_policy(
            scenarios[wc.scenario]
        )
    log:
        "logs/policy/apply_technical_potential_constraints_{scenario}.log"
    benchmark:
        "benchmarks/policy/apply_technical_potential_constraints_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario="[A-Za-z0-9_-]+"
    script:
        "../scripts/generators/apply_technical_potential_constraints.py"
