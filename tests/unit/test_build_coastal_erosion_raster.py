"""
Unit tests for build_coastal_erosion_raster.py — NCERM coastal erosion pipeline.

Tests the NCERM-based pipeline:
- Constants (default layer names)
- Input validation (file existence)
- Layer loading from multi-layer GeoPackage
- Rasterization to single-band binary GeoTIFF
- GeoTIFF round-trip (write and read back)
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import MultiPolygon, box

pytestmark = pytest.mark.unit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.land.build_coastal_erosion_raster import (
    BAND_NAME,
    DEFAULT_LAYERS,
    build_coastal_erosion_raster,
    validate_inputs,
)
from scripts.utilities.land_utils import (
    create_reference_grid,
    load_and_reproject_vector,
    rasterize_vector,
    write_geotiff,
)


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def small_bounds():
    """Small bounding box for fast test rasters (10km x 10km)."""
    return (400_000, 300_000, 410_000, 310_000)


@pytest.fixture
def gpkg_path(tmp_path):
    """
    Create a synthetic 3-layer GeoPackage mimicking NCERM 2024 structure.

    Each layer has MultiPolygon geometries in EPSG:27700 positioned within
    the small_bounds area. Layer 3 partially overlaps Layer 1 to test
    OR-merge behaviour.
    """
    gpkg = tmp_path / "coastal_erosion_uk_2024.gpkg"

    # Layer 1: NCERM_SMP_2105_70CC — erosion polygons (south-west of bounds)
    polys1 = [
        MultiPolygon([box(401_000, 301_000, 402_000, 302_000)]),
        MultiPolygon([box(402_500, 301_500, 403_500, 302_500)]),
    ]
    gdf1 = gpd.GeoDataFrame(
        {"frontageid": [1, 2]},
        geometry=polys1,
        crs="EPSG:27700",
    )
    gdf1.to_file(gpkg, layer="NCERM_SMP_2105_70CC", driver="GPKG")

    # Layer 2: NCERM_Ground_Instability_Zone — separate area (north-east)
    polys2 = [
        MultiPolygon([box(406_000, 306_000, 407_000, 307_000)]),
    ]
    gdf2 = gpd.GeoDataFrame(
        {"frontageid": [3]},
        geometry=polys2,
        crs="EPSG:27700",
    )
    gdf2.to_file(gpkg, layer="NCERM_Ground_Instability_Zone", driver="GPKG")

    # Layer 3: NCERM_Ground_Instability_Recession — overlaps with Layer 1
    polys3 = [
        MultiPolygon([box(401_500, 301_500, 402_500, 302_500)]),
    ]
    gdf3 = gpd.GeoDataFrame(
        {"frontageid": [4]},
        geometry=polys3,
        crs="EPSG:27700",
    )
    gdf3.to_file(gpkg, layer="NCERM_Ground_Instability_Recession", driver="GPKG")

    return str(gpkg)


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Constants
# ══════════════════════════════════════════════════════════════════════════════


class TestConstants:
    """Test that module constants are correctly defined."""

    def test_default_layers_count(self):
        """Test there are exactly 3 default layer names."""
        assert len(DEFAULT_LAYERS) == 3

    def test_default_layers_values(self):
        """Test layer names match expected NCERM layer names."""
        assert "NCERM_SMP_2105_70CC" in DEFAULT_LAYERS
        assert "NCERM_Ground_Instability_Zone" in DEFAULT_LAYERS
        assert "NCERM_Ground_Instability_Recession" in DEFAULT_LAYERS

    def test_band_name(self):
        """Test band name constant."""
        assert BAND_NAME == "coastal_erosion"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Input Validation
# ══════════════════════════════════════════════════════════════════════════════


class TestInputValidation:
    """Test input file validation."""

    def test_validate_inputs_exists(self, gpkg_path):
        """Test that validation passes when file exists."""
        validate_inputs({"coastal_erosion": gpkg_path})

    def test_validate_inputs_missing_raises(self, tmp_path):
        """Test that missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Missing 1 input file"):
            validate_inputs({"coastal_erosion": str(tmp_path / "nonexistent.gpkg")})


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Load Layers
# ══════════════════════════════════════════════════════════════════════════════


class TestLoadLayers:
    """Test loading named layers from the multi-layer GeoPackage."""

    def test_loads_named_layer(self, gpkg_path):
        """Test that a named layer can be loaded."""
        gdf = load_and_reproject_vector(gpkg_path, layer="NCERM_SMP_2105_70CC")
        assert isinstance(gdf, gpd.GeoDataFrame)
        assert len(gdf) == 2

    def test_crs_is_27700(self, gpkg_path):
        """Test that loaded data is in EPSG:27700."""
        gdf = load_and_reproject_vector(gpkg_path, layer="NCERM_SMP_2105_70CC")
        assert gdf.crs.to_epsg() == 27700

    def test_all_three_layers_loadable(self, gpkg_path):
        """Test that all 3 default layers can be loaded."""
        for layer_name in DEFAULT_LAYERS:
            gdf = load_and_reproject_vector(gpkg_path, layer=layer_name)
            assert len(gdf) > 0, f"Layer {layer_name} returned empty GeoDataFrame"

    def test_geometry_is_multipolygon(self, gpkg_path):
        """Test that geometries are MultiPolygon."""
        gdf = load_and_reproject_vector(gpkg_path, layer="NCERM_SMP_2105_70CC")
        assert all(gdf.geometry.geom_type == "MultiPolygon")


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Rasterization (single-band, component-level)
# ══════════════════════════════════════════════════════════════════════════════


class TestRasterizationComponents:
    """Test rasterization at the component level using land_utils directly."""

    @pytest.fixture
    def template(self, small_bounds):
        """Small reference grid template for test rasters."""
        return create_reference_grid(
            bounds=small_bounds, resolution=100, crs="EPSG:27700"
        )

    @pytest.fixture
    def erosion_gdf(self, gpkg_path):
        """Load a single layer for component-level rasterization tests."""
        return load_and_reproject_vector(gpkg_path, layer="NCERM_SMP_2105_70CC")

    def test_rasterize_single_layer(self, erosion_gdf, template):
        """Test that a single layer rasterizes to a 2D array."""
        result = rasterize_vector(erosion_gdf, template, burn_value=1, dtype="uint8")
        assert result.ndim == 2

    def test_rasterize_dtype_uint8(self, erosion_gdf, template):
        """Test that rasterized output is uint8."""
        result = rasterize_vector(erosion_gdf, template, burn_value=1, dtype="uint8")
        assert result.dtype == np.uint8

    def test_rasterize_binary_values(self, erosion_gdf, template):
        """Test that rasterized output contains only 0 and 1."""
        result = rasterize_vector(erosion_gdf, template, burn_value=1, dtype="uint8")
        assert set(np.unique(result)).issubset({0, 1})

    def test_rasterize_has_erosion_pixels(self, erosion_gdf, template):
        """Test that rasterization produces non-zero pixels."""
        result = rasterize_vector(erosion_gdf, template, burn_value=1, dtype="uint8")
        assert np.count_nonzero(result) > 0

    def test_or_merge_captures_all_layers(self, gpkg_path, template):
        """Test that OR-merge of 3 layers captures more pixels than any single layer."""
        accumulator = np.zeros(
            (template[1], template[0]), dtype="uint8"  # (height, width)
        )
        single_layer_pixels = []

        for layer_name in DEFAULT_LAYERS:
            gdf = load_and_reproject_vector(gpkg_path, layer=layer_name)
            burned = rasterize_vector(gdf, template, burn_value=1, dtype="uint8")
            single_layer_pixels.append(int(burned.sum()))
            np.maximum(accumulator, burned, out=accumulator)

        merged_pixels = int(accumulator.sum())
        max_single = max(single_layer_pixels)

        # Merged should be >= any single layer (OR semantics)
        assert merged_pixels >= max_single

    def test_overlapping_layers_idempotent(self, gpkg_path, template):
        """Test that overlapping polygons are OR-merged (max value is 1)."""
        accumulator = np.zeros(
            (template[1], template[0]), dtype="uint8"
        )

        for layer_name in DEFAULT_LAYERS:
            gdf = load_and_reproject_vector(gpkg_path, layer=layer_name)
            burned = rasterize_vector(gdf, template, burn_value=1, dtype="uint8")
            np.maximum(accumulator, burned, out=accumulator)

        assert accumulator.max() <= 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST: Build Function (integration-level)
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildCoastalErosionRaster:
    """Test the build_coastal_erosion_raster function end-to-end."""

    @pytest.fixture
    def erosion_result(self, gpkg_path, small_bounds):
        """Build erosion raster from test GPKG with small bounds."""
        return build_coastal_erosion_raster(
            input_path=gpkg_path,
            layers=DEFAULT_LAYERS,
            resolution=100,
            target_crs="EPSG:27700",
            bounds=small_bounds,
        )

    def test_returns_tuple(self, erosion_result):
        """Test that function returns (array, profile) tuple."""
        raster, profile = erosion_result
        assert isinstance(raster, np.ndarray)
        assert isinstance(profile, dict)

    def test_output_is_2d(self, erosion_result):
        """Test that output raster is 2D (single band)."""
        raster, _ = erosion_result
        assert raster.ndim == 2

    def test_dtype_uint8(self, erosion_result):
        """Test that output raster uses uint8 dtype."""
        raster, _ = erosion_result
        assert raster.dtype == np.uint8

    def test_binary_values_only(self, erosion_result):
        """Test that raster contains only 0 and 1 values."""
        raster, _ = erosion_result
        assert set(np.unique(raster)).issubset({0, 1})

    def test_has_erosion_pixels(self, erosion_result):
        """Test that raster has non-zero erosion pixels."""
        raster, _ = erosion_result
        assert np.count_nonzero(raster) > 0

    def test_profile_single_band(self, erosion_result):
        """Test that profile specifies single band."""
        _, profile = erosion_result
        assert profile["count"] == 1

    def test_profile_crs(self, erosion_result):
        """Test that profile CRS is EPSG:27700."""
        _, profile = erosion_result
        assert profile["crs"] == "EPSG:27700"

    def test_profile_dtype(self, erosion_result):
        """Test that profile dtype is uint8."""
        _, profile = erosion_result
        assert profile["dtype"] == "uint8"


# ══════════════════════════════════════════════════════════════════════════════
# TEST: GeoTIFF Round-Trip
# ══════════════════════════════════════════════════════════════════════════════


class TestGeoTIFFRoundTrip:
    """Test that written GeoTIFF can be read back correctly."""

    @pytest.fixture
    def written_geotiff(self, tmp_path, gpkg_path, small_bounds):
        """Build, write, and return (path, original_raster, profile)."""
        raster, profile = build_coastal_erosion_raster(
            input_path=gpkg_path,
            layers=DEFAULT_LAYERS,
            resolution=100,
            target_crs="EPSG:27700",
            bounds=small_bounds,
        )

        output_path = tmp_path / "coastal_erosion_gb.tif"
        write_geotiff(raster, profile, str(output_path), band_names=[BAND_NAME])
        return str(output_path), raster, profile

    def test_output_file_exists(self, written_geotiff):
        """Test that the output GeoTIFF file was created."""
        path, _, _ = written_geotiff
        assert Path(path).exists()

    def test_readable_with_rasterio(self, written_geotiff):
        """Test that the output GeoTIFF is valid and readable."""
        path, _, _ = written_geotiff
        with rasterio.open(path) as src:
            assert src.count == 1
            assert src.crs.to_epsg() == 27700
            assert src.dtypes[0] == "uint8"

    def test_data_matches_original(self, written_geotiff):
        """Test that data read back matches original raster."""
        path, original_raster, _ = written_geotiff
        with rasterio.open(path) as src:
            read_data = src.read(1)
            np.testing.assert_array_equal(read_data, original_raster)

    def test_band_description(self, written_geotiff):
        """Test that band description is set correctly."""
        path, _, _ = written_geotiff
        with rasterio.open(path) as src:
            assert src.descriptions[0] == BAND_NAME, (
                f"Band description: expected '{BAND_NAME}', "
                f"got '{src.descriptions[0]}'"
            )
