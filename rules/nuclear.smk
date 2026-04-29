"""
Nuclear metadata scaffolding for nuclear-to-hydrogen work.

This stage does not rebuild reactor assets. Instead it annotates existing
nuclear generators after storage integration so downstream hydrogen and future
heat-coupling logic can rely on stable reactor metadata.
"""

resources_path = "resources"


rule add_nuclear_metadata:
    """
    Annotate nuclear generators with reactor-class and heat-readiness metadata.

    Transforms:
      {scenario}_network_demand_renewables_thermal_generators_storage.pkl
      -> {scenario}_network_demand_renewables_thermal_generators_storage_nuclear.pkl
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_network_demand_renewables_thermal_generators_storage.pkl"
    output:
        network=f"{resources_path}/network/{{scenario}}_network_demand_renewables_thermal_generators_storage_nuclear.pkl"
    params:
        scenario=lambda wc: wc.scenario,
        scenario_config=lambda wc: scenarios[wc.scenario]
    message:
        "Annotating nuclear generators for {wildcards.scenario}"
    log:
        "logs/nuclear/add_nuclear_metadata_{scenario}.log"
    benchmark:
        "benchmarks/nuclear/add_nuclear_metadata_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario="[A-Za-z0-9_-]+"
    script:
        "../scripts/nuclear/add_nuclear_metadata.py"
