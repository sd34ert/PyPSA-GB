#!/usr/bin/env python3
"""
Apply ONR Semi-Urban Demographic Criterion for Nuclear Siting.

Determines which 100m grid squares in England & Wales are ineligible for
nuclear power station siting based on the ONR's cumulative weighted population
(CWP) criterion. Scotland is excluded entirely (nuclear ban).

A site meets the semi-urban demographic criteria if all ratios of actual
versus hypothetical cumulative weighted population values (Site Population
Factors) are less than unity, i.e. SPF_MAX < 1. A pixel that fails this
test is SMR-ineligible on demographic grounds.

The criterion has two tests, both of which must yield SPF < 1:

  1. All-around (360 degree): CWP_360(r) summed over all directions from
     0 to r km must not exceed CWP_bar_360(r) for a hypothetical uniform
     1000 persons/km^2 density, checked at every cumulative radius
     r = 2..30 km.  SPF_360(r) = CWP_360(r) / CWP_bar_360(r) < 1.

  2. Sector (30 degree): CWP_theta(r) summed within each 30-degree sector
     (72 sectors at 5-degree increments, theta = 0..355) must not exceed
     CWP_bar_30(r) for a hypothetical uniform 5000 persons/km^2 sector,
     checked at every cumulative radius r = 2..30 km.
     SPF_theta(r) = CWP_theta(r) / CWP_bar_30(r) < 1 for all theta, r.

Population within 1 km of the site centre is included in the actual CWP but
the hypothetical site has zero population within 1 km (ONR equations 1-10).

Weighting factors follow ONR Equation (2): rm = sqrt((r^2 + (r-1)^2) / 2),
and Equation (1): Wr = rm^{-1.5}. The 56.542 constant cancels in the SPF
ratio and is omitted.

Processing stages:
    1. Load population density raster and Scotland mask; validate alignment
    2. Zero population in Scotland before convolution (prevents Scottish
       population inflating English/Welsh border pixel CWPs)
    3. Phase 1: All-around criterion via 30 band FFT convolutions (~2 min)
    4. Phase 2: Sector criterion via parallel candidate-chunk processing
       (~25-30 min with 4 workers)
    5. Combine results, compute zone fractions, write outputs

Input:
    - resources/land/population_density_gb.tif — float32, people/km^2, 100m
    - resources/land/scotland_mask_{network_model}.tif — uint8 binary mask
    - Zone shapes geojson for the specified network model

Output:
    - resources/land/nuclear_pop_criterion_{network_model}.tif — uint8 binary
      mask (1 = ineligible, 0 = eligible/SMR-eligible, 255 = nodata/sea)
    - resources/land/nuclear_pop_criterion_fractions_{network_model}.csv —
      per-zone eligible fraction for downstream build_nuclear_eligibility

Author: K O'Neill
Date: 2026-03-25
"""

import logging
import sys
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize as rio_rasterize
from scipy.signal import fftconvolve

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


from scripts.utilities.land_utils import (
    calculate_zone_fraction,
    load_zone_shapes,
    validate_crs,
    write_geotiff,
)

# Get snakemake object if running under Snakemake
snk = globals().get("snakemake")

# Setup logging
log_path = (
    snk.log[0]
    if snk and hasattr(snk, "log") and snk.log
    else "nuclear_pop_density_criterion"
)
logger = setup_logging(log_path)


# =============================================================================
# ONR CONSTANTS
# =============================================================================

# Spatial constants — derived from foundation resolution in main()
# Default values for 100m resolution; overridden at runtime if config differs
PIX_PER_KM = 10  # 1000m / resolution_m
PIXEL_AREA_KM2 = 0.01  # (resolution_m / 1000)^2
MAX_R_KM = 30  # Maximum assessment radius
N_SLICES = 72  # 5-degree angular slices (360/5)
SECTOR_WIDTH = 6  # slices per 30-degree sector (30/5)


def compute_onr_constants():
    """
    Compute ONR demographic criterion constants from equations 1-8.

    Returns
    -------
    tuple of (dict, dict, dict)
        ``(Wr, T_360, T_30)`` where:
        - Wr[r] is the weighting factor for band r (r=1..30)
        - T_360[r] is the all-around cumulative threshold at radius r (r=2..30)
        - T_30[r] is the 30-degree sector cumulative threshold at radius r
    """
    Wr = {}
    for r in range(1, MAX_R_KM + 1):
        rm = np.sqrt((r**2 + (r - 1) ** 2) / 2)  # Equation (2)
        Wr[r] = rm ** (-1.5)  # Equation (1), 56.542 omitted

    # Cumulative hypothetical thresholds (Equations 5-8)
    # Band 1 (0-1km): zero population in hypothetical
    T_360 = {}
    T_30 = {}
    cum_360 = cum_30 = 0.0
    for r in range(2, MAX_R_KM + 1):
        band_area = np.pi * (r**2 - (r - 1) ** 2)
        cum_360 += Wr[r] * 1000 * band_area  # Equation (8): 1000 persons/km^2
        cum_30 += Wr[r] * 5000 * band_area / 12  # Equation (7): 5000/12
        T_360[r] = cum_360
        T_30[r] = cum_30

    return Wr, T_360, T_30


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def apply_scotland_exclusion(
    pop_density: np.ndarray,
    scotland_mask: np.ndarray,
    land_mask_raw: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Zero population in Scotland and create England & Wales land mask.

    Zeroing Scottish population BEFORE FFT convolution prevents it from
    inflating CWP values for English/Welsh border pixels within 30km of
    Scotland.

    Parameters
    ----------
    pop_density : np.ndarray
        Population density raster (float32, people/km^2). Modified in place.
    scotland_mask : np.ndarray
        Binary mask (uint8): 1 = Scotland, 0 = not Scotland.
    land_mask_raw : np.ndarray
        Binary mask of all land pixels (before Scotland exclusion).

    Returns
    -------
    tuple of (np.ndarray, np.ndarray)
        ``(pop_density, ew_land_mask)`` where pop_density has Scotland
        zeroed and ew_land_mask is True for England & Wales land pixels only.
    """
    sco_bool = scotland_mask.astype(bool)
    pop_density[sco_bool] = 0.0
    ew_land_mask = land_mask_raw & ~sco_bool
    return pop_density, ew_land_mask


def compute_allaround_criterion(
    pop_density: np.ndarray,
    land_mask: np.ndarray,
    Wr: dict,
    T_360: dict,
    T_30: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Phase 1: All-around criterion via 30 band FFT convolutions.

    For each annular band r=1..30, convolves population density with a
    weighted ring kernel, accumulates cumulative CWP, and checks
    SPF_360(r) < 1 at every radius r=2..30 km.

    Also identifies sector candidates (pixels that passed all-around but
    have CWP_360(r) > T_30(r) at some radius, meaning they could fail the
    sector check since sector CWP <= all-around CWP).

    Parameters
    ----------
    pop_density : np.ndarray
        Population density with Scotland zeroed (float32).
    land_mask : np.ndarray
        England & Wales land mask (bool).
    Wr : dict
        Weighting factors from :func:`compute_onr_constants`.
    T_360 : dict
        All-around cumulative thresholds.
    T_30 : dict
        Sector cumulative thresholds.

    Returns
    -------
    tuple of (np.ndarray, np.ndarray)
        ``(ineligible_allaround, sector_candidates_mask)`` — both boolean
        arrays of shape ``(H, W)``.
    """
    H, W = pop_density.shape
    ksize = 2 * MAX_R_KM * PIX_PER_KM + 1  # 601
    center = ksize // 2

    # Kernel pixel distances
    ky, kx = np.mgrid[-center : center + 1, -center : center + 1]
    dist_km = np.sqrt(kx**2 + ky**2) / PIX_PER_KM

    cum_cwp = np.zeros((H, W), dtype=np.float32)
    ineligible = np.zeros((H, W), dtype=bool)
    sector_candidate = np.zeros((H, W), dtype=bool)

    for r in range(1, MAX_R_KM + 1):
        ring_mask = (dist_km >= (r - 1)) & (dist_km < r)
        kernel = np.zeros((ksize, ksize), dtype=np.float32)
        kernel[ring_mask] = PIXEL_AREA_KM2 * Wr[r]

        band_cwp = fftconvolve(pop_density, kernel, mode="same").astype(np.float32)
        cum_cwp += band_cwp
        del band_cwp

        if r >= 2:
            ineligible |= (cum_cwp > T_360[r]) & land_mask
            sector_candidate |= (cum_cwp > T_30[r]) & land_mask

        if r in (1, 2, 5, 10, 15, 20, 25, 30):
            logger.info(
                f"  Band r={r:2d}km — all-around fails: {ineligible.sum():>10,} "
                f"— sector candidates: {sector_candidate.sum():>10,}"
            )

    sector_candidates_mask = sector_candidate & ~ineligible & land_mask
    return ineligible, sector_candidates_mask


def precompute_sector_offsets(
    max_r_km: int,
    pix_per_km: int,
) -> tuple[dict, int]:
    """
    Precompute ring offset tables for sector check, grouped by (band, slice).

    Parameters
    ----------
    max_r_km : int
        Maximum radius in km.
    pix_per_km : int
        Pixels per km (10 for 100m resolution).

    Returns
    -------
    tuple of (dict, int)
        ``(ring_slices, total_offsets)`` where ring_slices[r][s] = (dy, dx)
        arrays of int16 offsets for band r, 5-degree slice s.
    """
    ksize = 2 * max_r_km * pix_per_km + 1
    center = ksize // 2

    ky, kx = np.mgrid[-center : center + 1, -center : center + 1]
    dist_km = np.sqrt(kx**2 + ky**2) / pix_per_km
    angle_deg = np.degrees(np.arctan2(kx, -ky)) % 360

    band_of = np.floor(dist_km).astype(int) + 1  # band r covers (r-1) to r km
    slice_of = np.floor(angle_deg / 5).astype(int) % N_SLICES

    ring_slices = {}
    total_offsets = 0
    for r in range(1, max_r_km + 1):
        ring_slices[r] = {}
        for s in range(N_SLICES):
            mask = (band_of == r) & (slice_of == s) & (dist_km < max_r_km)
            dy = ky[mask].astype(np.int16)
            dx = kx[mask].astype(np.int16)
            ring_slices[r][s] = (dy, dx)
            total_offsets += len(dy)

    return ring_slices, total_offsets


# -- Multiprocessing worker globals and functions ----------------------------

_w_pop = None
_w_slices = None
_w_Wr = None
_w_T30 = None
_w_pad_r = None
_w_pad_c = None


def _set_worker_globals(pop_padded, slices, Wr, T30, pad_r, pad_c):
    """Set module globals for worker processes (called before fork)."""
    global _w_pop, _w_slices, _w_Wr, _w_T30, _w_pad_r, _w_pad_c
    _w_pop = pop_padded
    _w_slices = slices
    _w_Wr = Wr
    _w_T30 = T30
    _w_pad_r = pad_r
    _w_pad_c = pad_c


def _process_chunk(args):
    """
    Process a single candidate chunk through all bands and sectors.

    Parameters
    ----------
    args : tuple
        ``(c_start, c_end, pop_padded, slices, Wr, T30, pad_r, pad_c)`` — slice indices and worker globals.

    Returns
    -------
    tuple of (int, int, np.ndarray)
        ``(c_start, c_end, failed)`` where failed is a bool array of
        length ``c_end - c_start``. True where any SPF_theta(r) >= 1.
    """
    c_start, c_end, pop_padded, slices, Wr, T30, pad_r, pad_c = args
    ch_rows = pad_r[c_start:c_end]
    ch_cols = pad_c[c_start:c_end]
    ch_size = c_end - c_start

    cum_slc = np.zeros((ch_size, N_SLICES), dtype=np.float32)
    ext = np.zeros((ch_size, N_SLICES + SECTOR_WIDTH), dtype=np.float32)
    failed = np.zeros(ch_size, dtype=bool)

    for r in range(1, MAX_R_KM + 1):
        w = PIXEL_AREA_KM2 * Wr[r]
        for s in range(N_SLICES):
            dy_s, dx_s = slices[r][s]
            if len(dy_s) == 0:
                continue
            pop_vals = pop_padded[
                ch_rows[:, None] + dy_s[None, :],
                ch_cols[:, None] + dx_s[None, :],
            ]
            cum_slc[:, s] += pop_vals.sum(axis=1) * w

        if r >= 2:
            ext[:, :N_SLICES] = cum_slc
            ext[:, N_SLICES:] = cum_slc[:, :SECTOR_WIDTH]
            cs = np.cumsum(ext, axis=1)
            sector_sums = cs[:, SECTOR_WIDTH:] - cs[:, :N_SLICES]
            failed |= sector_sums.max(axis=1) > T30[r]

    return c_start, c_end, failed


def run_sector_check(
    pop_density: np.ndarray,
    sector_candidates_mask: np.ndarray,
    Wr: dict,
    T_30: dict,
    ring_slices: dict,
    n_workers: int,
    chunk_size: int = 10_000,
) -> np.ndarray:
    """
    Phase 2: Sector criterion via parallel candidate-chunk processing.

    For each candidate pixel that passed the all-around check, decomposes
    population into 72 five-degree angular slices band by band. At each
    cumulative radius r=2..30, checks if any 30-degree sector has
    SPF_theta(r) >= 1. Uses multiprocessing with shared memory for the
    padded population array.

    Parameters
    ----------
    pop_density : np.ndarray
        Population density with Scotland zeroed (float32).
    sector_candidates_mask : np.ndarray
        Boolean mask of pixels to check.
    Wr : dict
        Weighting factors.
    T_30 : dict
        Sector cumulative thresholds.
    ring_slices : dict
        Precomputed offset tables from :func:`precompute_sector_offsets`.
    n_workers : int
        Number of parallel worker processes.
    chunk_size : int, optional
        Candidates per chunk, by default 10,000.

    Returns
    -------
    np.ndarray
        Boolean mask of sector-ineligible pixels (same shape as input).
    """
    cand_rows, cand_cols = np.where(sector_candidates_mask)
    n_cand = len(cand_rows)

    if n_cand == 0:
        logger.info("No sector candidates — all-around caught everything.")
        return np.zeros_like(sector_candidates_mask)

    # Sort spatially for cache-friendly access
    sort_idx = np.lexsort((cand_cols, cand_rows))
    cand_rows = cand_rows[sort_idx]
    cand_cols = cand_cols[sort_idx]

    # Pad population raster
    pad = MAX_R_KM * PIX_PER_KM  # 300 pixels
    pop_padded = np.pad(pop_density, pad, constant_values=0.0)
    pad_rows = (cand_rows + pad).astype(np.int32)
    pad_cols = (cand_cols + pad).astype(np.int32)

    # Build chunk arguments
    chunks = []
    for start in range(0, n_cand, chunk_size):
        end = min(start + chunk_size, n_cand)
        chunks.append((start, end, pop_padded, ring_slices, Wr, T_30, pad_rows, pad_cols))

    n_chunks = len(chunks)
    inelig_flat = np.zeros(n_cand, dtype=bool)
    t0 = time.time()

    use_parallel = n_workers > 1 and n_cand > chunk_size

    try:
        if use_parallel:
            logger.info(
                f"Processing {n_cand:,} sector candidates in {n_chunks} chunks "
                f"across {n_workers} workers (fork)..."
            )
            # Use default multiprocessing context (spawn on Windows, fork on Unix)
            ctx = multiprocessing.get_context()
            with ProcessPoolExecutor(
                max_workers=n_workers, mp_context=ctx
            ) as executor:
                for i, (c_start, c_end, failed) in enumerate(
                    executor.map(_process_chunk, chunks)
                ):
                    inelig_flat[c_start:c_end] = failed
                    if (i + 1) % max(1, n_chunks // 10) == 0 or i == n_chunks - 1:
                        elapsed = time.time() - t0
                        done = i + 1
                        eta = (
                            elapsed / done * (n_chunks - done)
                            if done > 0
                            else 0
                        )
                        fails = int(inelig_flat[:c_end].sum())
                        logger.info(
                            f"  Chunk {done:>4d}/{n_chunks} — "
                            f"sector fails: {fails:>8,} — "
                            f"{elapsed:>5.0f}s elapsed, ~{eta:.0f}s remaining"
                        )
        else:
            logger.info(
                f"Processing {n_cand:,} sector candidates in {n_chunks} chunks "
                f"(single-process)..."
            )
            for i, (start, end) in enumerate(chunks):
                c_start, c_end, failed = _process_chunk((start, end, pop_padded, ring_slices, Wr, T_30, pad_rows, pad_cols))
                inelig_flat[c_start:c_end] = failed
                if (i + 1) % max(1, n_chunks // 10) == 0 or i == n_chunks - 1:
                    elapsed = time.time() - t0
                    fails = int(inelig_flat[:c_end].sum())
                    logger.info(
                        f"  Chunk {i+1:>4d}/{n_chunks} — "
                        f"sector fails: {fails:>8,} — "
                        f"{elapsed:>5.0f}s elapsed"
                    )
    finally:
        _set_worker_globals(None, None, None, None, None, None)

    # Map back to raster
    sector_ineligible = np.zeros_like(sector_candidates_mask)
    sector_ineligible[cand_rows[inelig_flat], cand_cols[inelig_flat]] = True

    return sector_ineligible


# =============================================================================
# MAIN PROCESSING FUNCTION
# =============================================================================


def build_nuclear_pop_criterion(
    pop_density_path: str,
    scotland_mask_path: str,
    zones_path: str,
    output_criterion_path: str,
    output_fractions_path: str,
    n_workers: int = 4,
    target_crs: str = "EPSG:27700",
) -> dict:
    """
    Build the ONR demographic criterion raster and zone fractions CSV.

    A pixel is SMR-eligible (output=0) if SPF_MAX < 1 across both the
    all-around and sector tests at every cumulative radius r=2..30 km.

    Parameters
    ----------
    pop_density_path : str
        Path to population density GeoTIFF (float32, people/km^2).
    scotland_mask_path : str
        Path to Scotland mask GeoTIFF (uint8, 1=Scotland).
    zones_path : str
        Path to zone shapes GeoJSON.
    output_criterion_path : str
        Path for output criterion GeoTIFF.
    output_fractions_path : str
        Path for output zone fractions CSV.
    n_workers : int, optional
        Parallel workers for sector check, by default 4.
    target_crs : str, optional
        Target CRS string, by default "EPSG:27700".

    Returns
    -------
    dict
        Stage timing dict for logging.
    """
    stage_times = {}

    # ── Stage 1: Load inputs ──
    stage_start = time.time()

    with rasterio.open(pop_density_path) as src:
        pop_density = src.read(1).astype(np.float32)
        pop_transform = src.transform
        pop_nodata = src.nodata
        land_mask_raw = src.read(1) != pop_nodata

    pop_density[pop_density == pop_nodata] = 0.0
    pop_density[pop_density < 0] = 0.0

    with rasterio.open(scotland_mask_path) as src:
        scotland_mask = src.read(1)
        validate_crs(
            gpd.GeoDataFrame(geometry=[], crs=src.crs),
            expected_crs=target_crs,
        )

    zones = load_zone_shapes(zones_path, target_crs=target_crs)

    logger.info(
        f"Population density: {pop_density.shape}, "
        f"range [0, {pop_density.max():.0f}] persons/km^2"
    )
    logger.info(f"Land pixels: {land_mask_raw.sum():,}")
    stage_times["Load inputs"] = time.time() - stage_start

    # ── Stage 2: Scotland exclusion ──
    stage_start = time.time()
    sco_bool = scotland_mask.astype(bool)
    pop_density, ew_land_mask = apply_scotland_exclusion(
        pop_density, scotland_mask, land_mask_raw
    )
    n_ew_land = int(ew_land_mask.sum())
    n_sco = int(sco_bool.sum())
    logger.info(
        f"Scotland exclusion: {n_sco:,} Scottish pixels zeroed, "
        f"{n_ew_land:,} E&W land pixels remain"
    )
    stage_times["Scotland exclusion"] = time.time() - stage_start

    # ── Stage 3: ONR constants ──
    Wr, T_360, T_30 = compute_onr_constants()
    logger.info(
        f"ONR thresholds at r=30km: T_360={T_360[MAX_R_KM]:,.0f}, "
        f"T_30={T_30[MAX_R_KM]:,.0f}"
    )

    # ── Stage 4: Phase 1 — All-around criterion ──
    stage_start = time.time()
    logger.info("Phase 1: All-around criterion (30 band FFT convolutions)...")
    ineligible_allaround, sector_candidates_mask = compute_allaround_criterion(
        pop_density, ew_land_mask, Wr, T_360, T_30
    )
    n_aa = int(ineligible_allaround.sum())
    n_sec_cand = int(sector_candidates_mask.sum())
    logger.info(
        f"Phase 1 complete: {n_aa:,} all-around ineligible "
        f"({n_aa / max(n_ew_land, 1) * 100:.2f}% of E&W land)"
    )
    logger.info(f"Sector candidates: {n_sec_cand:,}")
    stage_times["Phase 1: All-around"] = time.time() - stage_start

    # ── Stage 5: Phase 2 — Sector criterion ──
    stage_start = time.time()
    logger.info(
        "Phase 2: Sector criterion (parallel candidate-chunk processing)..."
    )

    ring_slices, total_offsets = precompute_sector_offsets(MAX_R_KM, PIX_PER_KM)
    logger.info(f"Precomputed {total_offsets:,} ring offsets")

    sector_ineligible = run_sector_check(
        pop_density,
        sector_candidates_mask,
        Wr,
        T_30,
        ring_slices,
        n_workers,
    )
    n_sec = int(sector_ineligible.sum())
    logger.info(
        f"Phase 2 complete: {n_sec:,} sector additions "
        f"({n_sec / max(n_ew_land, 1) * 100:.2f}% of E&W land)"
    )
    stage_times["Phase 2: Sector"] = time.time() - stage_start

    # ── Stage 6: Combine results ──
    stage_start = time.time()
    combined_ineligible = ineligible_allaround | sector_ineligible
    n_combined = int(combined_ineligible.sum())
    logger.info(
        f"Combined: {n_combined:,} ineligible E&W pixels "
        f"({n_combined / max(n_ew_land, 1) * 100:.2f}% of E&W land)"
    )

    # Build output raster: 0=eligible (SPF_MAX<1), 1=ineligible, 255=nodata
    H, W = pop_density.shape
    output_raster = np.full((H, W), 255, dtype=np.uint8)
    output_raster[sco_bool] = 1  # Scotland: ineligible (ban)
    output_raster[ew_land_mask & combined_ineligible] = 1  # E&W: SPF_MAX >= 1
    output_raster[ew_land_mask & ~combined_ineligible] = 0  # E&W: SPF_MAX < 1
    stage_times["Combine results"] = time.time() - stage_start

    # ── Stage 7: Zone fractions ──
    stage_start = time.time()
    eligible_mask = np.zeros((H, W), dtype=np.uint8)
    eligible_mask[ew_land_mask & ~combined_ineligible] = 1

    eligible_frac = calculate_zone_fraction(eligible_mask, zones, pop_transform)

    # Determine which zones are in Scotland (majority of zone pixels in mask)
    zone_raster = rio_rasterize(
        [(geom, i + 1) for i, geom in enumerate(zones.geometry)],
        out_shape=(H, W),
        transform=pop_transform,
        fill=0,
        dtype=np.uint16,
    )
    zone_lookup = dict(enumerate(zones["zone_name"], start=1))

    rows = []
    for zid, zname in sorted(zone_lookup.items()):
        zmask = zone_raster == zid
        z_total = int(zmask.sum())
        is_scottish = z_total > 0 and (zmask & sco_bool).sum() > (z_total * 0.5)
        frac = eligible_frac.get(zname, 0.0)
        rows.append(
            {
                "zone_name": zname,
                "pop_criterion_eligible_frac": round(float(frac), 4),
                "scotland_excluded": is_scottish,
            }
        )

    fractions_df = pd.DataFrame(rows)
    Path(output_fractions_path).parent.mkdir(parents=True, exist_ok=True)
    fractions_df.to_csv(output_fractions_path, index=False)
    logger.info(f"Wrote zone fractions to {output_fractions_path}")
    stage_times["Zone fractions"] = time.time() - stage_start

    # ── Stage 8: Write output GeoTIFF ──
    stage_start = time.time()
    out_profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": W,
        "height": H,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": pop_transform,
    }
    write_geotiff(
        output_raster,
        out_profile,
        output_criterion_path,
        band_names=["nuclear_pop_criterion"],
        nodata=255,
    )
    stage_times["Write GeoTIFF"] = time.time() - stage_start

    return stage_times


# =============================================================================
# MAIN EXECUTION
# =============================================================================


def main():
    """Main execution function."""
    start_time = time.time()

    logger.info("=" * 80)
    logger.info("NUCLEAR POPULATION DENSITY CRITERION (ONR Semi-Urban)")
    logger.info("=" * 80)

    # Get parameters
    global PIX_PER_KM, PIXEL_AREA_KM2

    if snk:
        pop_density_path = snk.input.pop_density
        scotland_mask_path = snk.input.scotland_mask
        zones_path = snk.input.zones
        output_criterion = snk.output.criterion
        output_fractions = snk.output.zone_fractions
        n_workers = snk.threads
        resolution_m = snk.params.resolution
        target_crs = f"EPSG:{snk.params.target_crs}"
    else:
        # Standalone defaults for testing (zonal network model)
        pop_density_path = "resources/land/population_density_gb.tif"
        scotland_mask_path = "resources/land/scotland_mask_zonal.tif"
        zones_path = "data/network/zonal/zones.geojson"
        output_criterion = "resources/land/nuclear_pop_criterion_zonal.tif"
        output_fractions = (
            "resources/land/nuclear_pop_criterion_fractions_zonal.csv"
        )
        n_workers = 4
        resolution_m = 100
        target_crs = "EPSG:27700"

    # Derive spatial constants from resolution
    PIX_PER_KM = int(1000 / resolution_m)
    PIXEL_AREA_KM2 = (resolution_m / 1000) ** 2

    logger.info(f"Population density: {pop_density_path}")
    logger.info(f"Scotland mask: {scotland_mask_path}")
    logger.info(f"Zones: {zones_path}")
    logger.info(f"Resolution: {resolution_m}m (PIX_PER_KM={PIX_PER_KM})")
    logger.info(f"Workers: {n_workers}")

    stage_times = build_nuclear_pop_criterion(
        pop_density_path=pop_density_path,
        scotland_mask_path=scotland_mask_path,
        zones_path=zones_path,
        output_criterion_path=output_criterion,
        output_fractions_path=output_fractions,
        n_workers=n_workers,
        target_crs=target_crs,
    )

    log_stage_summary(stage_times, logger)
    log_execution_summary(
        logger,
        script_name="Nuclear Population Density Criterion",
        start_time=start_time,
        inputs=snk.input if snk else None,
        outputs=snk.output if snk else None,
        context={
            "max_radius_km": MAX_R_KM,
            "n_workers": n_workers,
            "resolution_m": resolution_m,
        },
    )

    logger.info("=" * 80)
    logger.info("NUCLEAR POPULATION DENSITY CRITERION — COMPLETE")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
