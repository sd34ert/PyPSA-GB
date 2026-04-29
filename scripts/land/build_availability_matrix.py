#!/usr/bin/env python3
"""
Merge per-technology availability CSVs into a single unified matrix.

Reads per-technology availability CSVs (onshore renewables, nuclear SMR,
hydrogen) and combines them into a single availability_matrix CSV with
one row per zone and one column per technology.

This is a simple tabular merge — no spatial processing. Each input CSV
has a zone column and one or more availability fraction columns (0.0–1.0).
The merge is an outer join on zone so all zones appear even if a
technology CSV is missing.

Input:
    - resources/land/onshore_renewable_availability_{network_model}.csv
      (columns: zone_name, onwind, solar)
    - resources/land/smr_availability_{network_model}.csv
      (columns: zone, smr_available_frac)
    - resources/land/h2_availability_{network_model}.csv (FUTURE)

Output:
    - resources/land/availability_matrix_{network_model}.csv
      (columns: zone, onwind, solar, smr, [electrolysis, h2_turbine])

Author: K O'Neill
Date: 2026-03-29
"""

import logging
import sys
import time
from pathlib import Path

import pandas as pd

# Project path setup
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

try:
    from scripts.utilities.logging_config import (
        log_execution_summary,
        log_stage_summary,
        setup_logging,
    )
except ImportError:
    logging.basicConfig(level=logging.INFO)

    def setup_logging(name):
        return logging.getLogger(name)

    def log_stage_summary(*args, **kwargs):
        pass

    def log_execution_summary(*args, **kwargs):
        pass


# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "build_availability_matrix"
)
logger = setup_logging(log_path)


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()
    stage_times = {}

    logger.info("=" * 80)
    logger.info("BUILD AVAILABILITY MATRIX")
    logger.info("=" * 80)

    # Get parameters
    if snk:
        onshore_path = snk.input.onshore
        smr_path = snk.input.smr
        h2_path = getattr(snk.input, "hydrogen", None)
        output_path = snk.output.matrix
    else:
        onshore_path = "resources/land/onshore_renewable_availability_Zonal.csv"
        smr_path = "resources/land/smr_availability_Zonal.csv"
        h2_path = None
        output_path = "resources/land/availability_matrix_Zonal.csv"

    # Stage 1: Load onshore renewable availability
    stage_start = time.time()

    onshore_df = pd.read_csv(onshore_path)
    # Normalise zone column name
    if "zone_name" in onshore_df.columns:
        onshore_df = onshore_df.rename(columns={"zone_name": "zone"})
    logger.info(
        f"Onshore: {len(onshore_df)} zones, "
        f"technologies: {[c for c in onshore_df.columns if c != 'zone']}"
    )

    # Start with onshore as the base
    matrix = onshore_df.copy()

    stage_times["Load onshore"] = time.time() - stage_start

    # Stage 2: Merge SMR availability
    stage_start = time.time()

    if not Path(smr_path).exists():
        raise FileNotFoundError(f"SMR availability not found: {smr_path}")

    smr_df = pd.read_csv(smr_path)
    # Rename fraction column to 'smr'
    if "smr_available_frac" in smr_df.columns:
        smr_df = smr_df.rename(columns={"smr_available_frac": "smr"})
    # Normalise zone column
    if "zone_name" in smr_df.columns:
        smr_df = smr_df.rename(columns={"zone_name": "zone"})

    matrix = matrix.merge(smr_df[["zone", "smr"]], on="zone", how="left")
    # Zones not in SMR (e.g. offshore) get NaN → fill with 0.0
    matrix["smr"] = matrix["smr"].fillna(0.0)
    logger.info(f"SMR: {len(smr_df)} zones merged")

    stage_times["Merge SMR"] = time.time() - stage_start

    # Stage 3: Merge hydrogen availability (if available)
    stage_start = time.time()

    if h2_path and Path(h2_path).exists():
        h2_df = pd.read_csv(h2_path)
        if "zone_name" in h2_df.columns:
            h2_df = h2_df.rename(columns={"zone_name": "zone"})
        h2_cols = [c for c in h2_df.columns if c != "zone"]
        matrix = matrix.merge(h2_df[["zone"] + h2_cols], on="zone", how="left")
        for col in h2_cols:
            matrix[col] = matrix[col].fillna(0.0)
        logger.info(f"Hydrogen: {len(h2_df)} zones merged, columns: {h2_cols}")
    else:
        logger.info("Hydrogen: not available yet — skipped")

    stage_times["Merge hydrogen"] = time.time() - stage_start

    # Stage 4: Write output
    stage_start = time.time()

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(output_path, index=False)

    tech_cols = [c for c in matrix.columns if c != "zone"]
    logger.info(f"Availability matrix written: {output_path}")
    logger.info(f"  Zones: {len(matrix)}, Technologies: {tech_cols}")
    logger.info(f"\n{matrix.to_string(index=False)}")

    stage_times["Write output"] = time.time() - stage_start

    # Log summary
    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Build Availability Matrix",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "zones": len(matrix),
            "technologies": tech_cols,
        },
    )

    logger.info("=" * 80)
    logger.info("BUILD AVAILABILITY MATRIX — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
