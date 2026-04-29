"""
Post-Solve Analysis Rules for PyPSA-GB

This module contains all post-processing analysis rules for solved networks.

Pipeline Stages:
  1. analyze_and_visualize_solved_network - Interactive spatial plot + dashboard + JSON summary
  2. generate_location_constraint_report - Post-policy candidate cap/build report
  3. generate_analysis_notebook - Jupyter notebook for detailed analysis
  4. solve_scenario - Aggregate rule for complete workflow

Inputs:
  - Solved network (.nc file)
  - Generator summary data (.csv)

Outputs:
  - Interactive spatial plot (HTML)
  - Results dashboard (HTML)
  - Analysis summary (JSON)
  - Jupyter notebook (.ipynb)

See Also:
  - solve.smk - Produces solved networks that these rules analyze
  - scripts/analyze_solved_network.py - Spatial plotting and dashboard generation
  - scripts/generate_analysis_notebook.py - Notebook generation
"""

import re

# Use scenarios and run_ids from main Snakefile (no config reloading)
_scenarios = scenarios
_run_ids = run_ids

def _regex_from_list(items):
    """Generate regex pattern that matches exactly the items in the list."""
    if not items:
        return r"(?!x)x"  # Never-matching pattern
    return r"(?:%s)" % "|".join(map(re.escape, items))

SCENARIO_REGEX = _regex_from_list(_run_ids)


def _is_gb_cluster_quickwin_scenario(scenario_id):
    explicit_demand = scenarios.get(scenario_id, {}).get("hydrogen", {}).get("explicit_demand", {})
    nodes_path = explicit_demand.get("nodes_path", "")
    return "resources/case_studies/gb_cluster_n2h_2035/inputs/" in nodes_path


def _gb_cluster_quickwin_scenarios():
    return sorted([scenario_id for scenario_id in _scenarios if _is_gb_cluster_quickwin_scenario(scenario_id)])


def _gb_cluster_quickwin_active_scenarios():
    return [scenario_id for scenario_id in _run_ids if _is_gb_cluster_quickwin_scenario(scenario_id)]


GB_CLUSTER_SCENARIO_REGEX = _regex_from_list(_gb_cluster_quickwin_scenarios())


def _get_location_constraint_pre_policy_network(wildcards):
    """Return the assembled pre-policy network used to compare candidate headroom."""
    if _should_apply_technical_potential_constraints(wildcards.scenario):
        return (
            f"{resources_path}/network/{wildcards.scenario}"
            "_network_demand_renewables_thermal_generators_storage_nuclear_hydrogen_interconnectors.nc"
        )
    if _is_clustering_enabled(wildcards.scenario):
        return _clustered_network_output(wildcards.scenario)
    return (
        f"{resources_path}/network/{wildcards.scenario}"
        "_network_demand_renewables_thermal_generators_storage_nuclear_hydrogen_interconnectors.nc"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS AND VISUALIZATION RULES
# ══════════════════════════════════════════════════════════════════════════════

rule analyze_and_visualize_solved_network:
    """
    Comprehensive post-processing of solved network: plotting, visualization, and analysis.
    
    This consolidated rule combines all post-solve analysis tasks into one output:
    
    1. Interactive Spatial Plot (HTML):
       - Network topology with buses (color-coded by voltage level)
       - Transmission lines (color-coded by loading)
       - Generator locations (size/color by capacity and technology)
       - Storage units
       - Fully interactive Plotly map with pan/zoom/hover
       
    2. Results Dashboard (HTML):
       - Hourly generation mix (stacked area)
       - Peak generation by carrier (bar chart)
       - Storage state of charge (line chart for top 5 units)
       - Load shedding events (area chart)
       - Transmission line loading distribution (histogram)
       - System cost breakdown (pie chart)
       - All synchronized with hover interaction
       
    3. Analysis Summary (JSON):
       - Network size metrics (buses, lines, generators, etc.)
       - Results: total cost, generation, demand, load shedding
       - Generation by carrier (MWh)
       - Peak and average demand
       - Energy balance validation
    
    This replaces separate plotting.smk rules and notebooks.smk generation,
    providing a single, efficient post-processing step.
    
    Input:
      - Solved network: {scenario}_solved.nc
    
    Output:
      - Spatial interactive plot: {scenario}_spatial.html
      - Results dashboard: {scenario}_dashboard.html
      - Summary metrics: {scenario}_summary.json
    
    Performance:
      - ~10-30 seconds for full analysis
      - No re-solve needed
      - Can be run independently on existing solved networks
    
    Usage:
      snakemake resources/analysis/HT35_spatial.html --cores 1
      snakemake resources/analysis/HT35_dashboard.html --cores 1
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_solved.nc"
    output:
        spatial_plot=f"{resources_path}/analysis/{{scenario}}_spatial.html",
        dashboard=f"{resources_path}/analysis/{{scenario}}_dashboard.html",
        summary=f"{resources_path}/analysis/{{scenario}}_summary.json"
    log:
        "logs/analysis/analyze_solved_network_{scenario}.log"
    benchmark:
        "benchmarks/analysis/analyze_solved_network_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    script:
        "../scripts/analysis/analyze_solved_network.py"


rule generate_analysis_notebook:
    """
    Generate an interactive Jupyter notebook for detailed analysis of a solved network.
    
    Creates a notebook with:
    - Network summary and scenario parameters
    - Generation mix by technology (interactive plots)
    - Storage dispatch and state of charge
    - Transmission line loading analysis
    - Locational marginal prices (LMP)
    - Renewable curtailment analysis
    - Load shedding events
    
    This notebook provides deeper analysis capabilities than the HTML dashboard,
    allowing users to modify analysis and create custom visualizations.
    
    Input:
      - Solved network: {scenario}_solved.nc
      - Generator summary: {scenario}_generators_summary_by_carrier.csv
    
    Output:
      - Analysis notebook: {scenario}_notebook.ipynb
    
    Performance: ~5-10s
    
    Usage:
      snakemake resources/analysis/HT35_notebook.ipynb --cores 1
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_solved.nc",
        generators_summary=f"{resources_path}/generators/{{scenario}}_generators_summary_by_carrier.csv"
    output:
        notebook=f"{resources_path}/analysis/{{scenario}}_notebook.ipynb"
    params:
        scenario=lambda wc: wc.scenario
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    log:
        "logs/analysis/generate_analysis_notebook_{scenario}.log"
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/analysis/generate_analysis_notebook.py"


rule generate_location_constraint_report:
    """
    Generate a post-policy, solved-aware row-level report for future candidate rows.

    This report joins the pre-policy candidate envelope, finalized post-policy
    network, and solved build results so constrained and unconstrained scenarios
    are easy to compare without manual multi-file inspection.
    """
    input:
        pre_policy_network=_get_location_constraint_pre_policy_network,
        post_policy_network=f"{resources_path}/network/{{scenario}}.nc",
        solved_network=f"{resources_path}/network/{{scenario}}_solved.nc",
        pre_policy_report=f"{resources_path}/generators/{{scenario}}_future_capacity_oversubscription.csv",
    output:
        report=f"{resources_path}/analysis/{{scenario}}_location_constraint_report.csv"
    log:
        "logs/analysis/generate_location_constraint_report_{scenario}.log"
    benchmark:
        "benchmarks/analysis/generate_location_constraint_report_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    script:
        "../scripts/analysis/generate_location_constraint_report.py"


rule generate_scotland_scenario_audit:
    """
    Generate a Scotland scenario-audit table from the finalized pre-solve network.

    This audit verifies scenario identity and provenance before solved results
    are interpreted in the paper workflow.
    """
    input:
        network=f"{resources_path}/network/{{scenario}}.nc"
    output:
        audit=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_scenario_audit.csv",
        report=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_scenario_audit.md"
    params:
        scenario=lambda wc: wc.scenario,
        scenario_config=lambda wc: scenarios[wc.scenario]
    log:
        "logs/analysis/generate_scotland_scenario_audit_{scenario}.log"
    benchmark:
        "benchmarks/analysis/generate_scotland_scenario_audit_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    script:
        "../scripts/analysis/generate_scotland_scenario_audit.py"


rule generate_scotland_h2_service_trace:
    """
    Generate Scotland-focused hydrogen-service tracing outputs from a solved network.

    These diagnostics support both a strict Scottish-supply interpretation and a
    looser GB-backbone interpretation of Scottish explicit hydrogen demand service.
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_solved.nc"
    output:
        trace=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_h2_service_trace.csv",
        node_balance=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_h2_node_balance.csv",
        report=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_h2_service_trace.md"
    params:
        scenario=lambda wc: wc.scenario,
        scenario_config=lambda wc: scenarios[wc.scenario]
    log:
        "logs/analysis/generate_scotland_h2_service_trace_{scenario}.log"
    benchmark:
        "benchmarks/analysis/generate_scotland_h2_service_trace_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    script:
        "../scripts/analysis/generate_scotland_h2_service_trace.py"


rule generate_scotland_metrics_report:
    """
    Generate Scotland-focused adequacy and system-value metrics from a GB-integrated solve.

    The solved network boundary remains Great Britain. This study-specific output
    extracts Scotland as the analytical region and reports hydrogen adequacy,
    Scottish curtailment, Scottish nuclear output, and supporting GB system cost.
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_solved.nc"
    output:
        metrics=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_scotland_metrics.csv",
        report=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_scotland_metrics.md"
    params:
        scenario=lambda wc: wc.scenario,
        scenario_config=lambda wc: scenarios[wc.scenario]
    log:
        "logs/analysis/generate_scotland_metrics_{scenario}.log"
    benchmark:
        "benchmarks/analysis/generate_scotland_metrics_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    script:
        "../scripts/analysis/generate_scotland_metrics.py"


rule generate_hub_economics:
    """
    Generate hub-level marginal-price, average-cost proxy, curtailment exposure,
    and storage-cycle diagnostics from a solved network.
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_solved.nc"
    output:
        hub_marginal_prices=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_hub_marginal_prices.csv",
        hub_marginal_price_summary=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_hub_marginal_price_summary.csv",
        hub_average_cost_proxy=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_hub_average_cost_proxy.csv",
        cost_decomposition=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_cost_decomposition.csv",
        curtailment_market_exposure=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_curtailment_market_exposure.csv",
        storage_cycles=f"{resources_path}/case_studies/scotland_n2h_2035/analysis/{{scenario}}_storage_cycles.csv"
    params:
        scenario=lambda wc: wc.scenario,
        scenario_config=lambda wc: scenarios[wc.scenario]
    log:
        "logs/analysis/generate_hub_economics_{scenario}.log"
    benchmark:
        "benchmarks/analysis/generate_hub_economics_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=SCENARIO_REGEX
    script:
        "../scripts/analysis/generate_hub_economics.py"


rule generate_gb_cluster_quickwin_metrics:
    """
    Generate quick-win industrial-cluster hydrogen metrics from a solved network.
    """
    input:
        network=f"{resources_path}/network/{{scenario}}_solved.nc",
        cluster_definitions=f"{resources_path}/case_studies/gb_cluster_n2h_2035/inputs/cluster_definitions.csv"
    output:
        metrics=f"{resources_path}/case_studies/gb_cluster_n2h_2035/analysis/{{scenario}}_cluster_metrics.csv",
        node_balance=f"{resources_path}/case_studies/gb_cluster_n2h_2035/analysis/{{scenario}}_cluster_node_balance.csv",
        report=f"{resources_path}/case_studies/gb_cluster_n2h_2035/analysis/{{scenario}}_cluster_report.md"
    params:
        scenario=lambda wc: wc.scenario,
        scenario_config=lambda wc: scenarios[wc.scenario]
    log:
        "logs/analysis/generate_gb_cluster_quickwin_metrics_{scenario}.log"
    benchmark:
        "benchmarks/analysis/generate_gb_cluster_quickwin_metrics_{scenario}.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    wildcard_constraints:
        scenario=GB_CLUSTER_SCENARIO_REGEX
    script:
        "../scripts/analysis/generate_gb_cluster_metrics.py"


rule generate_gb_cluster_quickwin_comparator_report:
    """
    Generate a combined quick-win comparator report for active GB cluster scenarios.
    """
    input:
        metrics=expand(
            f"{resources_path}/case_studies/gb_cluster_n2h_2035/analysis/{{scenario}}_cluster_metrics.csv",
            scenario=_gb_cluster_quickwin_active_scenarios(),
        )
    output:
        metrics=f"{resources_path}/case_studies/gb_cluster_n2h_2035/analysis/gb_cluster_n2h_2035_quickwin_comparator_metrics.csv",
        report=f"{resources_path}/case_studies/gb_cluster_n2h_2035/analysis/gb_cluster_n2h_2035_quickwin_comparator_report.md"
    log:
        "logs/analysis/generate_gb_cluster_quickwin_comparator_report.log"
    benchmark:
        "benchmarks/analysis/generate_gb_cluster_quickwin_comparator_report.txt"
    conda:
        "../envs/pypsa-gb.yaml"
    script:
        "../scripts/analysis/generate_gb_cluster_quickwin_comparator_report.py"


# ══════════════════════════════════════════════════════════════════════════════
# AGGREGATE RULES
# ══════════════════════════════════════════════════════════════════════════════

# NOTE: To run full analysis for a scenario, use:
#   snakemake resources/analysis/{scenario}_spatial.html --cores 4
#   (this will automatically trigger solve → analysis → notebook)
#
# Or request specific outputs:
#   snakemake resources/analysis/HT35_spatial.html resources/analysis/HT35_notebook.ipynb --cores 4
