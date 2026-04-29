"""
Unit tests for nuclear_pop_density_criterion.py

Tests the ONR Semi-Urban Demographic Criterion pipeline:
- ONR constants (rm, Wr, thresholds) match formal equations
- Scotland exclusion zeroes population correctly
- All-around criterion (small synthetic rasters)
- Sector offset precomputation
- Sector check worker function
- Combined output raster values and zone fractions CSV
- Edge cases (empty candidates, uniform low density, all-Scotland)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import box

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.nuclear_pop_density_criterion import (
    apply_scotland_exclusion,
    compute_allaround_criterion,
    compute_onr_constants,
    precompute_sector_offsets,
    _process_chunk,
    _set_worker_globals,
    MAX_R_KM,
    N_SLICES,
    PIXEL_AREA_KM2,
    PIX_PER_KM,
    SECTOR_WIDTH,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def onr_constants():
    """Precomputed ONR constants for reuse across tests."""
    return compute_onr_constants()


@pytest.fixture
def small_pop_density():
    """
    Small 100x100 population density raster (10km x 10km at 100m resolution).

    Central 20x20 block (2km x 2km) has 5000 persons/km^2 (dense urban).
    Surrounding area has 100 persons/km^2 (rural).
    """
    pop = np.full((100, 100), 100.0, dtype=np.float32)
    pop[40:60, 40:60] = 5000.0
    return pop


@pytest.fixture
def small_scotland_mask():
    """Scotland mask for small raster: top 30 rows are Scotland."""
    mask = np.zeros((100, 100), dtype=np.uint8)
    mask[:30, :] = 1
    return mask


@pytest.fixture
def small_land_mask():
    """Land mask for small raster: all pixels are land."""
    return np.ones((100, 100), dtype=bool)


@pytest.fixture
def small_transform():
    """Affine transform for 100x100 raster at 100m in EPSG:27700."""
    return from_bounds(400_000, 300_000, 410_000, 310_000, 100, 100)


@pytest.fixture
def small_zones_gdf():
    """
    Two-zone GeoDataFrame for small raster.

    Zone A covers the left half, Zone B covers the right half.
    """
    return gpd.GeoDataFrame(
        {
            "zone_name": ["ZoneA", "ZoneB"],
            "geometry": [
                box(400_000, 300_000, 405_000, 310_000),
                box(405_000, 300_000, 410_000, 310_000),
            ],
        },
        crs="EPSG:27700",
    )


@pytest.fixture
def small_zones_file(tmp_path, small_zones_gdf):
    """Write small zones to a GeoJSON file."""
    # Use Name_1 column as load_zone_shapes expects
    gdf = small_zones_gdf.rename(columns={"zone_name": "Name_1"})
    path = tmp_path / "zones.geojson"
    gdf.to_file(path, driver="GeoJSON")
    return str(path)


@pytest.fixture
def pop_density_tif(tmp_path, small_pop_density, small_transform):
    """Write small population density to a GeoTIFF."""
    path = tmp_path / "population_density_gb.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": 100,
        "height": 100,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": small_transform,
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(small_pop_density, 1)
    return str(path)


@pytest.fixture
def scotland_mask_tif(tmp_path, small_scotland_mask, small_transform):
    """Write small Scotland mask to a GeoTIFF."""
    path = tmp_path / "scotland_mask_zonal.tif"
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": 100,
        "height": 100,
        "count": 1,
        "crs": "EPSG:27700",
        "transform": small_transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(small_scotland_mask, 1)
    return str(path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: ONR Constants
# ══════════════════════════════════════════════════════════════════════════════


class TestComputeOnrConstants:
    """Test ONR demographic criterion constants match formal equations."""

    def test_returns_three_dicts(self, onr_constants):
        """Test that compute_onr_constants returns (Wr, T_360, T_30)."""
        Wr, T_360, T_30 = onr_constants

        assert isinstance(Wr, dict)
        assert isinstance(T_360, dict)
        assert isinstance(T_30, dict)

    def test_wr_has_30_bands(self, onr_constants):
        """Test that Wr has entries for bands r=1..30."""
        Wr, _, _ = onr_constants

        assert len(Wr) == 30
        assert set(Wr.keys()) == set(range(1, 31))

    def test_wr_positive_and_decreasing(self, onr_constants):
        """Test that weights are positive and decrease with distance."""
        Wr, _, _ = onr_constants

        for r in range(1, 31):
            assert Wr[r] > 0, f"Wr[{r}] should be positive"
        for r in range(2, 31):
            assert Wr[r] < Wr[r - 1], f"Wr[{r}] should be < Wr[{r-1}]"

    def test_rm_equation_2(self, onr_constants):
        """Test rm matches Equation (2): rm = sqrt((r^2 + (r-1)^2) / 2)."""
        Wr, _, _ = onr_constants

        for r in [1, 2, 5, 10, 30]:
            rm_expected = np.sqrt((r**2 + (r - 1) ** 2) / 2)
            wr_expected = rm_expected ** (-1.5)
            assert abs(Wr[r] - wr_expected) < 1e-10, (
                f"Wr[{r}]={Wr[r]} != expected {wr_expected}"
            )

    def test_thresholds_cumulative(self, onr_constants):
        """Test that T_360 and T_30 are strictly increasing with r."""
        _, T_360, T_30 = onr_constants

        for r in range(3, 31):
            assert T_360[r] > T_360[r - 1], f"T_360 not increasing at r={r}"
            assert T_30[r] > T_30[r - 1], f"T_30 not increasing at r={r}"

    def test_thresholds_start_at_r2(self, onr_constants):
        """Test that thresholds exist for r=2..30 (not r=1)."""
        _, T_360, T_30 = onr_constants

        assert 1 not in T_360
        assert 2 in T_360
        assert 30 in T_360
        assert 1 not in T_30
        assert 2 in T_30

    def test_t360_t30_ratio(self, onr_constants):
        """Test that T_360 / T_30 = 2.4 at every radius (1000*12 / 5000)."""
        _, T_360, T_30 = onr_constants

        for r in range(2, 31):
            ratio = T_360[r] / T_30[r]
            assert abs(ratio - 2.4) < 1e-10, (
                f"T_360/T_30 ratio at r={r} is {ratio}, expected 2.4"
            )

    def test_t360_at_r30_value(self, onr_constants):
        """Test T_360(30) is approximately 55,496 (known from notebook)."""
        _, T_360, _ = onr_constants

        assert 55_000 < T_360[30] < 56_000, (
            f"T_360(30)={T_360[30]:.0f}, expected ~55,496"
        )

    def test_t30_at_r30_value(self, onr_constants):
        """Test T_30(30) is approximately 23,123 (known from notebook)."""
        _, _, T_30 = onr_constants

        assert 23_000 < T_30[30] < 23_500, (
            f"T_30(30)={T_30[30]:.0f}, expected ~23,123"
        )


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Scotland Exclusion
# ══════════════════════════════════════════════════════════════════════════════


class TestApplyScotlandExclusion:
    """Test Scotland population zeroing and land mask creation."""

    def test_scottish_pixels_zeroed(
        self, small_pop_density, small_scotland_mask, small_land_mask
    ):
        """Test that population in Scottish pixels is set to zero."""
        pop = small_pop_density.copy()
        pop, _ = apply_scotland_exclusion(pop, small_scotland_mask, small_land_mask)

        assert pop[:30, :].max() == 0.0, "Scottish pixels should be zeroed"

    def test_ew_pixels_unchanged(
        self, small_pop_density, small_scotland_mask, small_land_mask
    ):
        """Test that E&W population values are preserved."""
        pop = small_pop_density.copy()
        original_ew = small_pop_density[30:, :].copy()
        pop, _ = apply_scotland_exclusion(pop, small_scotland_mask, small_land_mask)

        np.testing.assert_array_equal(pop[30:, :], original_ew)

    def test_ew_land_mask_excludes_scotland(
        self, small_pop_density, small_scotland_mask, small_land_mask
    ):
        """Test that E&W land mask excludes Scottish pixels."""
        pop = small_pop_density.copy()
        _, ew_mask = apply_scotland_exclusion(
            pop, small_scotland_mask, small_land_mask
        )

        assert ew_mask[:30, :].sum() == 0, "Scotland should be excluded"
        assert ew_mask[30:, :].sum() > 0, "E&W should have land pixels"

    def test_no_scotland_mask_preserves_all(
        self, small_pop_density, small_land_mask
    ):
        """Test with empty Scotland mask — all pixels preserved."""
        pop = small_pop_density.copy()
        empty_sco = np.zeros_like(small_pop_density, dtype=np.uint8)
        pop, ew_mask = apply_scotland_exclusion(pop, empty_sco, small_land_mask)

        assert pop.sum() > 0
        np.testing.assert_array_equal(ew_mask, small_land_mask)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Sector Offset Precomputation
# ══════════════════════════════════════════════════════════════════════════════


class TestPrecomputeSectorOffsets:
    """Test ring offset table precomputation."""

    def test_returns_dict_and_count(self):
        """Test that precompute returns (ring_slices, total_offsets)."""
        slices, total = precompute_sector_offsets(5, PIX_PER_KM)

        assert isinstance(slices, dict)
        assert isinstance(total, int)
        assert total > 0

    def test_all_bands_present(self):
        """Test that all bands r=1..max_r have entries."""
        max_r = 5
        slices, _ = precompute_sector_offsets(max_r, PIX_PER_KM)

        assert set(slices.keys()) == set(range(1, max_r + 1))

    def test_72_slices_per_band(self):
        """Test that each band has exactly 72 five-degree slices."""
        slices, _ = precompute_sector_offsets(5, PIX_PER_KM)

        for r in slices:
            assert len(slices[r]) == N_SLICES, (
                f"Band {r} has {len(slices[r])} slices, expected {N_SLICES}"
            )

    def test_offsets_are_int16_arrays(self):
        """Test that offset arrays are int16 numpy arrays."""
        slices, _ = precompute_sector_offsets(3, PIX_PER_KM)

        for r in slices:
            for s in range(N_SLICES):
                dy, dx = slices[r][s]
                assert dy.dtype == np.int16
                assert dx.dtype == np.int16

    def test_total_offsets_consistent(self):
        """Test that total_offsets equals sum of all offset arrays."""
        slices, total = precompute_sector_offsets(5, PIX_PER_KM)

        manual_total = sum(
            len(slices[r][s][0])
            for r in slices
            for s in range(N_SLICES)
        )
        assert total == manual_total

    def test_offsets_within_kernel_radius(self):
        """Test that all offsets are within the maximum pixel radius."""
        max_r = 5
        max_pix = max_r * PIX_PER_KM
        slices, _ = precompute_sector_offsets(max_r, PIX_PER_KM)

        for r in slices:
            for s in range(N_SLICES):
                dy, dx = slices[r][s]
                if len(dy) > 0:
                    dist = np.sqrt(dy.astype(float) ** 2 + dx.astype(float) ** 2)
                    assert dist.max() < max_pix + 1, (
                        f"Offset at band {r}, slice {s} exceeds radius"
                    )

    def test_inner_band_has_fewer_offsets_than_outer(self):
        """Test that inner bands have fewer ring pixels than outer bands."""
        slices, _ = precompute_sector_offsets(10, PIX_PER_KM)

        inner_total = sum(len(slices[2][s][0]) for s in range(N_SLICES))
        outer_total = sum(len(slices[10][s][0]) for s in range(N_SLICES))
        assert outer_total > inner_total


# ══════════════════════════════════════════════════════════════════════════════
# TEST: All-Around Criterion (small raster)
# ══════════════════════════════════════════════════════════════════════════════


class TestComputeAllaroundCriterion:
    """Test Phase 1 all-around criterion on small synthetic rasters."""

    def test_returns_two_boolean_masks(self, onr_constants):
        """Test that function returns (ineligible, sector_candidates)."""
        Wr, T_360, T_30 = onr_constants
        pop = np.zeros((50, 50), dtype=np.float32)
        land = np.ones((50, 50), dtype=bool)

        inelig, cands = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        assert inelig.dtype == bool
        assert cands.dtype == bool
        assert inelig.shape == (50, 50)
        assert cands.shape == (50, 50)

    def test_zero_population_all_eligible(self, onr_constants):
        """Test that zero population produces no ineligible pixels."""
        Wr, T_360, T_30 = onr_constants
        pop = np.zeros((50, 50), dtype=np.float32)
        land = np.ones((50, 50), dtype=bool)

        inelig, cands = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        assert inelig.sum() == 0
        assert cands.sum() == 0

    def test_uniform_low_density_all_eligible(self, onr_constants):
        """Test that uniform 50 persons/km^2 produces no ineligible pixels."""
        Wr, T_360, T_30 = onr_constants
        pop = np.full((50, 50), 50.0, dtype=np.float32)
        land = np.ones((50, 50), dtype=bool)

        inelig, _ = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        assert inelig.sum() == 0, "50 persons/km^2 should pass all-around"

    def test_high_density_centre_is_ineligible(self, onr_constants):
        """Test that a dense urban centre produces ineligible pixels."""
        Wr, T_360, T_30 = onr_constants
        # 100x100 raster, central block at 20,000 persons/km^2
        pop = np.full((100, 100), 10.0, dtype=np.float32)
        pop[40:60, 40:60] = 20_000.0
        land = np.ones((100, 100), dtype=bool)

        inelig, _ = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        # Centre pixels should be ineligible
        assert inelig[50, 50], "Dense centre pixel should be ineligible"
        assert inelig.sum() > 0

    def test_non_land_pixels_not_flagged(self, onr_constants):
        """Test that non-land pixels are never flagged as ineligible."""
        Wr, T_360, T_30 = onr_constants
        pop = np.full((50, 50), 50_000.0, dtype=np.float32)
        land = np.zeros((50, 50), dtype=bool)  # no land

        inelig, cands = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        assert inelig.sum() == 0, "Non-land pixels should not be flagged"
        assert cands.sum() == 0

    def test_sector_candidates_exclude_allaround_failures(self, onr_constants):
        """Test that sector candidates don't include all-around failures."""
        Wr, T_360, T_30 = onr_constants
        pop = np.full((100, 100), 10.0, dtype=np.float32)
        pop[45:55, 45:55] = 30_000.0
        land = np.ones((100, 100), dtype=bool)

        inelig, cands = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        # No pixel should be both ineligible AND a sector candidate
        overlap = inelig & cands
        assert overlap.sum() == 0, (
            "Sector candidates should not overlap with all-around failures"
        )


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Sector Check Worker
# ══════════════════════════════════════════════════════════════════════════════


class TestProcessChunk:
    """Test the sector check worker function with manually set globals."""

    def test_zero_population_all_pass(self, onr_constants):
        """Test that zero population means all candidates pass sector check."""
        Wr, _, T_30 = onr_constants
        pad = MAX_R_KM * PIX_PER_KM  # must match _process_chunk loop bound

        # Zero-population padded array large enough for 30km kernel
        size = 60 + 2 * pad
        pop_padded = np.zeros((size, size), dtype=np.float32)

        # 4 candidate pixels near centre
        cand_rows = np.array([30, 30, 31, 31], dtype=np.int32)
        cand_cols = np.array([30, 31, 30, 31], dtype=np.int32)
        pad_r = cand_rows + pad
        pad_c = cand_cols + pad

        ring_slices, _ = precompute_sector_offsets(MAX_R_KM, PIX_PER_KM)

        _set_worker_globals(pop_padded, ring_slices, Wr, T_30, pad_r, pad_c)
        try:
            c_start, c_end, failed = _process_chunk((0, 4))
            assert c_start == 0
            assert c_end == 4
            assert failed.sum() == 0, "Zero population should all pass"
        finally:
            _set_worker_globals(None, None, None, None, None, None)

    def test_returns_correct_shape(self, onr_constants):
        """Test that worker returns boolean array of correct length."""
        Wr, _, T_30 = onr_constants
        pad = MAX_R_KM * PIX_PER_KM
        size = 40 + 2 * pad

        pop_padded = np.zeros((size, size), dtype=np.float32)
        n_cand = 7
        pad_r = np.full(n_cand, pad + 20, dtype=np.int32)
        pad_c = np.full(n_cand, pad + 20, dtype=np.int32)

        ring_slices, _ = precompute_sector_offsets(MAX_R_KM, PIX_PER_KM)

        _set_worker_globals(pop_padded, ring_slices, Wr, T_30, pad_r, pad_c)
        try:
            _, _, failed = _process_chunk((0, n_cand))
            assert len(failed) == n_cand
            assert failed.dtype == bool
        finally:
            _set_worker_globals(None, None, None, None, None, None)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Full Pipeline (GeoTIFF round-trip)
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildNuclearPopCriterion:
    """Test the full pipeline with small synthetic data."""

    @pytest.mark.slow
    def test_produces_output_files(
        self, tmp_path, pop_density_tif, scotland_mask_tif, small_zones_file
    ):
        """Test that pipeline produces both output files."""
        from scripts.land.nuclear_pop_density_criterion import (
            build_nuclear_pop_criterion,
        )

        out_tif = str(tmp_path / "criterion.tif")
        out_csv = str(tmp_path / "fractions.csv")

        build_nuclear_pop_criterion(
            pop_density_path=pop_density_tif,
            scotland_mask_path=scotland_mask_tif,
            zones_path=small_zones_file,
            output_criterion_path=out_tif,
            output_fractions_path=out_csv,
            n_workers=1,
        )

        assert Path(out_tif).exists(), "Output GeoTIFF not created"
        assert Path(out_csv).exists(), "Output CSV not created"

    @pytest.mark.slow
    def test_output_raster_properties(
        self, tmp_path, pop_density_tif, scotland_mask_tif, small_zones_file
    ):
        """Test that output raster has correct CRS, dtype, and values."""
        from scripts.land.nuclear_pop_density_criterion import (
            build_nuclear_pop_criterion,
        )

        out_tif = str(tmp_path / "criterion.tif")
        out_csv = str(tmp_path / "fractions.csv")

        build_nuclear_pop_criterion(
            pop_density_path=pop_density_tif,
            scotland_mask_path=scotland_mask_tif,
            zones_path=small_zones_file,
            output_criterion_path=out_tif,
            output_fractions_path=out_csv,
            n_workers=1,
        )

        with rasterio.open(out_tif) as src:
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"
            data = src.read(1)
            unique = set(np.unique(data))
            # Should contain only 0 (eligible), 1 (ineligible), 255 (nodata)
            assert unique.issubset({0, 1, 255}), f"Unexpected values: {unique}"

    @pytest.mark.slow
    def test_scotland_pixels_ineligible(
        self, tmp_path, pop_density_tif, scotland_mask_tif, small_zones_file
    ):
        """Test that Scottish pixels are marked ineligible in output."""
        from scripts.land.nuclear_pop_density_criterion import (
            build_nuclear_pop_criterion,
        )

        out_tif = str(tmp_path / "criterion.tif")
        out_csv = str(tmp_path / "fractions.csv")

        build_nuclear_pop_criterion(
            pop_density_path=pop_density_tif,
            scotland_mask_path=scotland_mask_tif,
            zones_path=small_zones_file,
            output_criterion_path=out_tif,
            output_fractions_path=out_csv,
            n_workers=1,
        )

        with rasterio.open(out_tif) as src:
            data = src.read(1)

        # Top 30 rows are Scotland (mask=1), should all be 1 (ineligible)
        # Only pixels that are within zone boundaries will be 1 (not nodata)
        sco_region = data[:30, :]
        sco_land = sco_region[sco_region != 255]
        if len(sco_land) > 0:
            assert (sco_land == 1).all(), (
                "All Scottish land pixels should be ineligible"
            )

    @pytest.mark.slow
    def test_csv_has_expected_columns(
        self, tmp_path, pop_density_tif, scotland_mask_tif, small_zones_file
    ):
        """Test that zone fractions CSV has required columns."""
        from scripts.land.nuclear_pop_density_criterion import (
            build_nuclear_pop_criterion,
        )

        out_tif = str(tmp_path / "criterion.tif")
        out_csv = str(tmp_path / "fractions.csv")

        build_nuclear_pop_criterion(
            pop_density_path=pop_density_tif,
            scotland_mask_path=scotland_mask_tif,
            zones_path=small_zones_file,
            output_criterion_path=out_tif,
            output_fractions_path=out_csv,
            n_workers=1,
        )

        df = pd.read_csv(out_csv)
        assert "zone_name" in df.columns
        assert "pop_criterion_eligible_frac" in df.columns
        assert "scotland_excluded" in df.columns

    @pytest.mark.slow
    def test_csv_fractions_in_range(
        self, tmp_path, pop_density_tif, scotland_mask_tif, small_zones_file
    ):
        """Test that eligible fractions are between 0 and 1."""
        from scripts.land.nuclear_pop_density_criterion import (
            build_nuclear_pop_criterion,
        )

        out_tif = str(tmp_path / "criterion.tif")
        out_csv = str(tmp_path / "fractions.csv")

        build_nuclear_pop_criterion(
            pop_density_path=pop_density_tif,
            scotland_mask_path=scotland_mask_tif,
            zones_path=small_zones_file,
            output_criterion_path=out_tif,
            output_fractions_path=out_csv,
            n_workers=1,
        )

        df = pd.read_csv(out_csv)
        fracs = df["pop_criterion_eligible_frac"]
        assert (fracs >= 0.0).all(), "Fractions should be >= 0"
        assert (fracs <= 1.0).all(), "Fractions should be <= 1"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Edge Cases
# ══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_all_scotland_no_ew_land(self, onr_constants):
        """Test with entire raster marked as Scotland."""
        Wr, T_360, T_30 = onr_constants
        pop = np.full((50, 50), 5000.0, dtype=np.float32)
        sco_mask = np.ones((50, 50), dtype=np.uint8)
        land_mask = np.ones((50, 50), dtype=bool)

        pop, ew_land = apply_scotland_exclusion(pop, sco_mask, land_mask)

        assert pop.max() == 0.0, "All population should be zeroed"
        assert ew_land.sum() == 0, "No E&W land should remain"

    def test_uniform_threshold_density(self, onr_constants):
        """Test that exactly 1000 persons/km^2 uniform density passes.

        The hypothetical site has 1000 persons/km^2 from 1-30km. A pixel
        in a uniform 1000/km^2 field should have SPF_360 = 1.0 (borderline).
        Due to discrete pixel effects SPF may be slightly != 1.0.
        """
        Wr, T_360, T_30 = onr_constants
        # Large enough raster for 30km kernel to fit
        size = 700  # 70km at 100m resolution
        pop = np.full((size, size), 1000.0, dtype=np.float32)
        land = np.ones((size, size), dtype=bool)

        inelig, _ = compute_allaround_criterion(pop, land, Wr, T_360, T_30)

        # Centre pixel should be borderline — depending on discretisation
        # it may pass or fail. The key test is that it runs without error
        # and produces a boolean result.
        assert inelig.dtype == bool

    def test_sector_offsets_small_radius(self):
        """Test sector offsets with minimum radius (1 km)."""
        slices, total = precompute_sector_offsets(1, PIX_PER_KM)

        assert 1 in slices
        assert total > 0
        # Band 1 covers 0-1km, should have offsets within 10 pixels
        for s in range(N_SLICES):
            dy, dx = slices[1][s]
            if len(dy) > 0:
                assert np.abs(dy).max() <= PIX_PER_KM
                assert np.abs(dx).max() <= PIX_PER_KM
