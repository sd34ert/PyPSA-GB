"""
Unit tests for filter_smr_site_area.py — minimum contiguous site area filtering.

Tests the filter_contiguous_areas function which labels connected components
(8-connectivity) and removes regions smaller than min_site_area.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.filter_smr_site_area import filter_contiguous_areas


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def grid_with_small_and_large():
    """20x20 grid with a 3x3 cluster (small) and a 12x12 cluster (large).

    At 100m resolution:
    - 3x3 = 9 pixels = 0.09 km²
    - 12x12 = 144 pixels = 1.44 km²
    """
    grid = np.zeros((20, 20), dtype=np.uint8)
    # Small cluster at top-left
    grid[0:3, 0:3] = 1
    # Large cluster at bottom-right
    grid[8:20, 8:20] = 1
    return grid


@pytest.fixture
def grid_all_small():
    """Grid with only small clusters (each 2x2 = 4 pixels = 0.04 km²)."""
    grid = np.zeros((20, 20), dtype=np.uint8)
    # Three separate 2x2 clusters
    grid[0:2, 0:2] = 1
    grid[0:2, 5:7] = 1
    grid[5:7, 0:2] = 1
    return grid


@pytest.fixture
def grid_diagonal():
    """Grid with diagonally connected pixels (should be one region with 8-conn)."""
    grid = np.zeros((10, 10), dtype=np.uint8)
    # Diagonal line: (0,0), (1,1), (2,2), (3,3), (4,4)
    for i in range(5):
        grid[i, i] = 1
    return grid


# ── Tests ────────────────────────────────────────────────────────────────────


class TestFilterContiguousAreas:
    """Tests for the filter_contiguous_areas function."""

    def test_removes_small_regions(self, grid_with_small_and_large):
        """Small cluster (9 px = 0.09 km²) removed at 1.0 km² threshold."""
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=1.0, resolution_m=100
        )
        # Large cluster should remain
        assert filtered[10, 10] == 1
        # Small cluster should be removed
        assert filtered[1, 1] == 0
        assert n_total == 2
        assert n_valid == 1

    def test_preserves_large_regions(self, grid_with_small_and_large):
        """Large cluster (144 px = 1.44 km²) preserved at 1.0 km² threshold."""
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=1.0, resolution_m=100
        )
        # All pixels of the large cluster should remain
        assert filtered[8:20, 8:20].sum() == 144

    def test_region_exactly_at_threshold(self):
        """Region with exactly min_pixels should pass (>= not >)."""
        # 10x10 = 100 pixels = 1.0 km² at 100m resolution
        grid = np.zeros((20, 20), dtype=np.uint8)
        grid[5:15, 5:15] = 1
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid, min_area_km2=1.0, resolution_m=100
        )
        assert filtered[5:15, 5:15].sum() == 100
        assert n_valid == 1

    def test_zero_threshold_noop(self, grid_with_small_and_large):
        """min_area_km2=0 means no filtering — all pixels pass."""
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=0, resolution_m=100
        )
        np.testing.assert_array_equal(filtered, grid_with_small_and_large)
        assert n_total == n_valid

    def test_negative_threshold_noop(self, grid_with_small_and_large):
        """Negative min_area_km2 treated same as zero — no filtering."""
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=-1.0, resolution_m=100
        )
        np.testing.assert_array_equal(filtered, grid_with_small_and_large)

    def test_all_removed(self, grid_all_small):
        """All clusters below threshold → all zeros."""
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid_all_small, min_area_km2=1.0, resolution_m=100
        )
        assert filtered.sum() == 0
        assert n_total == 3
        assert n_valid == 0

    def test_empty_grid(self):
        """All-zero grid → no regions, returns unchanged."""
        grid = np.zeros((10, 10), dtype=np.uint8)
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid, min_area_km2=1.0, resolution_m=100
        )
        assert filtered.sum() == 0
        assert n_total == 0
        assert n_valid == 0

    def test_8_connectivity(self, grid_diagonal):
        """Diagonally connected pixels form one region with 8-connectivity."""
        filtered, n_total, n_valid = filter_contiguous_areas(
            grid_diagonal, min_area_km2=0.001, resolution_m=100
        )
        # 5 diagonal pixels should be one region
        assert n_total == 1
        # At 0.001 km² threshold (1 pixel), it should pass
        assert filtered.sum() == 5

    def test_4_connectivity_would_split(self, grid_diagonal):
        """Verify 8-connectivity is used: diagonal pixels are one region, not five."""
        _, n_total, _ = filter_contiguous_areas(
            grid_diagonal, min_area_km2=0.0, resolution_m=100
        )
        # With 8-connectivity, the 5 diagonal pixels form 1 region
        assert n_total == 1

    def test_output_dtype(self, grid_with_small_and_large):
        """Output is uint8."""
        filtered, _, _ = filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=1.0, resolution_m=100
        )
        assert filtered.dtype == np.uint8

    def test_output_shape(self, grid_with_small_and_large):
        """Output has same shape as input."""
        filtered, _, _ = filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=1.0, resolution_m=100
        )
        assert filtered.shape == grid_with_small_and_large.shape

    def test_does_not_modify_input(self, grid_with_small_and_large):
        """Input array is not modified in place."""
        original = grid_with_small_and_large.copy()
        filter_contiguous_areas(
            grid_with_small_and_large, min_area_km2=1.0, resolution_m=100
        )
        np.testing.assert_array_equal(grid_with_small_and_large, original)

    def test_different_resolution(self):
        """min_pixels scales correctly with resolution.

        At 50m resolution, 1 km² = 400 pixels (vs 100 at 100m).
        A 15x15 = 225 pixel cluster should fail at 50m but pass at 100m.
        """
        grid = np.zeros((20, 20), dtype=np.uint8)
        grid[0:15, 0:15] = 1  # 225 pixels

        # At 100m: 1 km² = 100 pixels → 225 passes
        filtered_100, _, n_valid_100 = filter_contiguous_areas(
            grid, min_area_km2=1.0, resolution_m=100
        )
        assert n_valid_100 == 1

        # At 50m: 1 km² = 400 pixels → 225 fails
        filtered_50, _, n_valid_50 = filter_contiguous_areas(
            grid, min_area_km2=1.0, resolution_m=50
        )
        assert n_valid_50 == 0
