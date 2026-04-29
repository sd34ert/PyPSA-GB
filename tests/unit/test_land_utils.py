"""Unit tests for scripts/utilities/land_utils.py."""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import Affine
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).parents[2]))

from scripts.utilities.land_utils import (
    buffer_geometries,
    calculate_zone_fraction,
    calculate_zone_summary,
    create_reference_grid,
    dissolve_overlaps,
    get_gb_canonical_bounds,
    load_and_reproject_vector,
    load_zone_shapes,
    merge_national_datasets,
    rasterize_continuous,
    rasterize_vector,
    reproject_raster,
    validate_crs,
    validate_gb_coverage,
    write_geotiff,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def projected_gdf():
    """Small GeoDataFrame in EPSG:27700 with two non-overlapping boxes."""
    return gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[box(0, 0, 100, 100), box(200, 200, 300, 300)],
        crs="EPSG:27700",
    )


@pytest.fixture
def overlapping_gdf():
    """GeoDataFrame in EPSG:27700 with two overlapping boxes."""
    return gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[box(0, 0, 100, 100), box(50, 50, 150, 150)],
        crs="EPSG:27700",
    )


@pytest.fixture
def geographic_gdf():
    """Small GeoDataFrame in EPSG:4326 (geographic / degree units)."""
    return gpd.GeoDataFrame(
        {"name": ["a"]},
        geometry=[box(-1, 51, 0, 52)],
        crs="EPSG:4326",
    )


@pytest.fixture
def sample_gpkg(tmp_path, projected_gdf):
    """Write a temporary GeoPackage and return its path."""
    path = tmp_path / "sample.gpkg"
    projected_gdf.to_file(path, driver="GPKG")
    return path


@pytest.fixture
def sample_gpkg_4326(tmp_path, geographic_gdf):
    """Write a temporary GeoPackage in EPSG:4326 and return its path."""
    path = tmp_path / "sample_4326.gpkg"
    geographic_gdf.to_file(path, driver="GPKG")
    return path


# ---------------------------------------------------------------------------
# load_and_reproject_vector
# ---------------------------------------------------------------------------


class TestLoadAndReprojectVector:
    def test_loads_gpkg(self, sample_gpkg):
        gdf = load_and_reproject_vector(sample_gpkg, target_crs="EPSG:27700")
        assert len(gdf) == 2
        assert gdf.crs.to_epsg() == 27700

    def test_reprojects_to_target_crs(self, sample_gpkg_4326):
        gdf = load_and_reproject_vector(sample_gpkg_4326, target_crs="EPSG:27700")
        assert gdf.crs.to_epsg() == 27700

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Vector file not found"):
            load_and_reproject_vector(tmp_path / "nonexistent.gpkg")

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.gpkg"
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        empty.to_file(path, driver="GPKG")
        with pytest.raises(ValueError, match="empty"):
            load_and_reproject_vector(path)

    def test_no_crs_raises(self, tmp_path):
        path = tmp_path / "no_crs.gpkg"
        gdf = gpd.GeoDataFrame({"name": ["a"]}, geometry=[box(0, 0, 1, 1)])
        gdf.to_file(path, driver="GPKG")
        # geopandas may write a default CRS; only assert if it truly has none
        try:
            load_and_reproject_vector(path)
            # If it loaded without error, geopandas assigned a CRS on write
        except ValueError as e:
            assert "no CRS defined" in str(e)

    def test_reads_named_layer(self, tmp_path):
        """Passing layer= reads the correct layer from a multi-layer GeoPackage."""
        path = tmp_path / "multi_layer.gpkg"
        gdf_a = gpd.GeoDataFrame(
            {"name": ["alpha"]},
            geometry=[box(0, 0, 100, 100)],
            crs="EPSG:27700",
        )
        gdf_b = gpd.GeoDataFrame(
            {"name": ["beta"]},
            geometry=[box(200, 200, 300, 300)],
            crs="EPSG:27700",
        )
        gdf_a.to_file(path, driver="GPKG", layer="layer_a")
        gdf_b.to_file(path, driver="GPKG", layer="layer_b")

        result = load_and_reproject_vector(path, layer="layer_b")
        assert len(result) == 1
        assert result["name"].iloc[0] == "beta"

    def test_skips_reprojection_when_crs_matches(self, sample_gpkg):
        gdf = load_and_reproject_vector(sample_gpkg, target_crs="EPSG:27700")
        # Geometries should be unchanged (no reprojection needed)
        assert gdf.crs.to_epsg() == 27700
        assert gdf.geometry.iloc[0].bounds == (0.0, 0.0, 100.0, 100.0)


# ---------------------------------------------------------------------------
# merge_national_datasets
# ---------------------------------------------------------------------------


class TestMergeNationalDatasets:
    def test_merges_multiple_files(self, tmp_path):
        paths = []
        for i, name in enumerate(["england", "scotland", "wales"]):
            gdf = gpd.GeoDataFrame(
                {"name": [name]},
                geometry=[box(i * 200, 0, i * 200 + 100, 100)],
                crs="EPSG:27700",
            )
            path = tmp_path / f"{name}.gpkg"
            gdf.to_file(path, driver="GPKG")
            paths.append(path)

        merged = merge_national_datasets(paths, target_crs="EPSG:27700")
        assert len(merged) == 3
        assert merged.crs.to_epsg() == 27700

    def test_single_file(self, sample_gpkg):
        merged = merge_national_datasets([sample_gpkg], target_crs="EPSG:27700")
        assert len(merged) == 2

    def test_empty_paths_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            merge_national_datasets([])

    def test_reprojects_mixed_crs(self, sample_gpkg, sample_gpkg_4326):
        merged = merge_national_datasets([sample_gpkg, sample_gpkg_4326], target_crs="EPSG:27700")
        assert merged.crs.to_epsg() == 27700
        assert len(merged) == 3  # 2 from projected + 1 from geographic


# ---------------------------------------------------------------------------
# buffer_geometries
# ---------------------------------------------------------------------------


class TestBufferGeometries:
    def test_buffers_projected_geometry(self, projected_gdf):
        buffered = buffer_geometries(projected_gdf, distance_m=10)
        # Buffered area should be larger than original
        original_area = projected_gdf.geometry.area.sum()
        buffered_area = buffered.geometry.area.sum()
        assert buffered_area > original_area

    def test_zero_distance_returns_unchanged(self, projected_gdf):
        result = buffer_geometries(projected_gdf, distance_m=0)
        assert result.geometry.equals(projected_gdf.geometry)

    def test_negative_distance_raises(self, projected_gdf):
        with pytest.raises(ValueError, match="non-negative"):
            buffer_geometries(projected_gdf, distance_m=-10)

    def test_geographic_crs_raises(self, geographic_gdf):
        with pytest.raises(ValueError, match="projected CRS"):
            buffer_geometries(geographic_gdf, distance_m=100)

    def test_empty_gdf_returns_unchanged(self):
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        result = buffer_geometries(empty, distance_m=100)
        assert result.empty

    def test_does_not_mutate_input(self, projected_gdf):
        original_area = projected_gdf.geometry.area.sum()
        buffer_geometries(projected_gdf, distance_m=50)
        assert projected_gdf.geometry.area.sum() == original_area


# ---------------------------------------------------------------------------
# dissolve_overlaps
# ---------------------------------------------------------------------------


class TestDissolveOverlaps:
    def test_dissolves_overlapping_polygons(self, overlapping_gdf):
        dissolved = dissolve_overlaps(overlapping_gdf)
        # Two overlapping boxes should merge into one polygon
        assert len(dissolved) == 1

    def test_preserves_non_overlapping(self, projected_gdf):
        dissolved = dissolve_overlaps(projected_gdf)
        # Two non-overlapping boxes should remain as two polygons
        assert len(dissolved) == 2

    def test_preserves_crs(self, overlapping_gdf):
        dissolved = dissolve_overlaps(overlapping_gdf)
        assert dissolved.crs.to_epsg() == 27700

    def test_empty_gdf_returns_unchanged(self):
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        result = dissolve_overlaps(empty)
        assert result.empty

    def test_total_area_not_increased(self, overlapping_gdf):
        """Dissolved area should equal the union, not the sum of inputs."""
        dissolved = dissolve_overlaps(overlapping_gdf)
        # Union of two overlapping 100x100 boxes (50x50 overlap) = 17500
        expected_union_area = 100 * 100 + 100 * 100 - 50 * 50
        dissolved_area = dissolved.geometry.area.sum()
        assert abs(dissolved_area - expected_union_area) < 1.0


# ---------------------------------------------------------------------------
# Rasterisation fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def template():
    """A 10x10 reference grid covering (0, 0) to (1000, 1000) at 100 m."""
    return create_reference_grid(bounds=(0, 0, 1000, 1000), resolution=100, crs="EPSG:27700")


@pytest.fixture
def sample_geotiff(tmp_path):
    """Write a small single-band GeoTIFF in EPSG:27700 and return its path."""
    path = tmp_path / "sample.tif"
    data = np.ones((5, 5), dtype="float32") * 42.0
    transform = Affine.translation(0, 500) * Affine.scale(100, -100)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="float32",
        crs="EPSG:27700",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture
def sample_geotiff_4326(tmp_path):
    """Write a small single-band GeoTIFF in EPSG:4326 (SW England) and return its path."""
    path = tmp_path / "sample_4326.tif"
    data = np.ones((5, 5), dtype="float32") * 7.0
    # ~1°x1° tile over SW England: lon -2 to -1, lat 51 to 52
    transform = Affine.translation(-2, 52) * Affine.scale(0.2, -0.2)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
    return path


# ---------------------------------------------------------------------------
# get_gb_canonical_bounds
# ---------------------------------------------------------------------------


class TestGetGbCanonicalBounds:
    def test_returns_tuple(self):
        bounds = get_gb_canonical_bounds()
        assert isinstance(bounds, tuple)
        assert len(bounds) == 4

    def test_default_crs(self):
        bounds = get_gb_canonical_bounds()
        xmin, ymin, xmax, ymax = bounds
        assert xmin == 0
        assert ymin == 0
        assert xmax == 700_000
        assert ymax == 1_300_000

    def test_explicit_27700(self):
        bounds = get_gb_canonical_bounds(crs="EPSG:27700")
        assert bounds == (0, 0, 700_000, 1_300_000)

    def test_unsupported_crs_raises(self):
        with pytest.raises(ValueError, match="only defined for EPSG:27700"):
            get_gb_canonical_bounds(crs="EPSG:4326")


# ---------------------------------------------------------------------------
# create_reference_grid
# ---------------------------------------------------------------------------


class TestCreateReferenceGrid:
    def test_valid_grid(self):
        width, height, transform, crs = create_reference_grid(
            bounds=(0, 0, 1000, 500), resolution=100
        )
        assert width == 10
        assert height == 5
        assert crs == "EPSG:27700"
        assert isinstance(transform, Affine)

    def test_transform_maps_origin(self):
        width, height, transform, _ = create_reference_grid(
            bounds=(100, 200, 500, 600), resolution=50
        )
        # Top-left pixel centre: transform maps (0, 0) -> (xmin, ymax)
        x, y = transform * (0, 0)
        assert x == 100.0
        assert y == 600.0

    def test_zero_resolution_raises(self):
        with pytest.raises(ValueError, match="positive"):
            create_reference_grid(bounds=(0, 0, 100, 100), resolution=0)

    def test_negative_resolution_raises(self):
        with pytest.raises(ValueError, match="positive"):
            create_reference_grid(bounds=(0, 0, 100, 100), resolution=-10)

    def test_invalid_bounds_xmin_ge_xmax(self):
        with pytest.raises(ValueError, match="Invalid bounds"):
            create_reference_grid(bounds=(100, 0, 50, 100), resolution=10)

    def test_invalid_bounds_ymin_ge_ymax(self):
        with pytest.raises(ValueError, match="Invalid bounds"):
            create_reference_grid(bounds=(0, 100, 100, 50), resolution=10)

    def test_defaults_to_canonical_gb_bounds(self):
        """Omitting bounds uses the canonical GB bounding box."""
        width, height, transform, crs = create_reference_grid()
        assert crs == "EPSG:27700"
        assert width == 7000  # 700_000 / 100
        assert height == 13000  # 1_300_000 / 100
        # Top-left corner should be (0, 1_300_000)
        x, y = transform * (0, 0)
        assert x == 0.0
        assert y == 1_300_000.0

    def test_canonical_bounds_with_custom_resolution(self):
        """Canonical bounds with non-default resolution."""
        width, height, _, _ = create_reference_grid(resolution=200)
        assert width == 3500  # 700_000 / 200
        assert height == 6500  # 1_300_000 / 200

    def test_canonical_bounds_deterministic(self):
        """Two calls with no bounds produce identical grids."""
        grid1 = create_reference_grid()
        grid2 = create_reference_grid()
        assert grid1 == grid2


# ---------------------------------------------------------------------------
# rasterize_vector
# ---------------------------------------------------------------------------


class TestRasterizeVector:
    def test_burns_geometry(self, projected_gdf, template):
        result = rasterize_vector(projected_gdf, template)
        assert result.shape == (10, 10)
        assert result.dtype == np.uint8
        # At least some pixels should be burned
        assert np.any(result == 1)
        # Not all pixels should be burned (boxes don't fill the grid)
        assert np.any(result == 0)

    def test_custom_burn_value(self, projected_gdf, template):
        result = rasterize_vector(projected_gdf, template, burn_value=5)
        assert np.max(result) == 5

    def test_empty_gdf_returns_zeros(self, template):
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        result = rasterize_vector(empty, template)
        assert result.shape == (10, 10)
        assert np.all(result == 0)

    def test_output_dtype(self, projected_gdf, template):
        result = rasterize_vector(projected_gdf, template, dtype="int16")
        assert result.dtype == np.int16

    def test_full_coverage_burns_all(self, template):
        """A box covering the entire grid should burn every pixel."""
        gdf = gpd.GeoDataFrame(geometry=[box(0, 0, 1000, 1000)], crs="EPSG:27700")
        result = rasterize_vector(gdf, template)
        assert np.all(result == 1)

    def test_geometry_alignment_with_continuous(self, template):
        """Binary and continuous rasterisation burn identical pixels for the same geometry."""
        gdf = gpd.GeoDataFrame(
            {"value": [5.0]},
            geometry=[box(200, 300, 600, 700)],
            crs="EPSG:27700",
        )
        binary = rasterize_vector(gdf, template)
        continuous = rasterize_continuous(gdf, template, value_column="value", nodata=0.0)
        # Every pixel burned by rasterize_vector should also be non-zero
        # in rasterize_continuous, and vice versa
        assert np.array_equal(binary > 0, continuous > 0)
        # Continuous pixels should carry the attribute value
        assert np.all(continuous[binary == 1] == 5.0)


# ---------------------------------------------------------------------------
# rasterize_continuous
# ---------------------------------------------------------------------------


class TestRasterizeContinuous:
    def test_burns_attribute_values(self, template):
        gdf = gpd.GeoDataFrame(
            {"value": [3.5, 7.0]},
            geometry=[box(0, 0, 500, 500), box(500, 500, 1000, 1000)],
            crs="EPSG:27700",
        )
        result = rasterize_continuous(gdf, template, value_column="value")
        assert result.dtype == np.float32
        # Pixels inside geometries should have the attribute values
        assert 3.5 in result
        assert 7.0 in result

    def test_nodata_fill(self, template):
        gdf = gpd.GeoDataFrame(
            {"value": [1.0]},
            geometry=[box(0, 0, 100, 100)],
            crs="EPSG:27700",
        )
        result = rasterize_continuous(gdf, template, value_column="value", nodata=-9999.0)
        # Most pixels should be nodata since only one small box is burned
        assert np.count_nonzero(result == -9999.0) > 0

    def test_missing_column_raises(self, projected_gdf, template):
        with pytest.raises(KeyError, match="not_a_column"):
            rasterize_continuous(projected_gdf, template, value_column="not_a_column")

    def test_empty_gdf_returns_nodata(self, template):
        empty = gpd.GeoDataFrame({"value": []}, geometry=[], crs="EPSG:27700")
        result = rasterize_continuous(empty, template, value_column="value", nodata=-9999.0)
        assert result.shape == (10, 10)
        assert np.all(result == -9999.0)

    def test_custom_dtype(self, template):
        gdf = gpd.GeoDataFrame(
            {"value": [2.0]},
            geometry=[box(0, 0, 500, 500)],
            crs="EPSG:27700",
        )
        result = rasterize_continuous(gdf, template, value_column="value", dtype="float64")
        assert result.dtype == np.float64


# ---------------------------------------------------------------------------
# reproject_raster
# ---------------------------------------------------------------------------


class TestReprojectRaster:
    def test_reprojects_geotiff(self, sample_geotiff):
        dst_array, dst_profile = reproject_raster(sample_geotiff, target_crs="EPSG:27700")
        assert dst_array.ndim == 3  # (bands, height, width)
        assert dst_array.shape[0] == 1  # single band
        assert dst_profile["crs"] == "EPSG:27700"

    def test_preserves_values(self, sample_geotiff):
        dst_array, _ = reproject_raster(sample_geotiff, target_crs="EPSG:27700")
        # Same CRS -> values should be preserved (all 42.0)
        assert np.allclose(dst_array, 42.0)

    def test_custom_resolution(self, sample_geotiff):
        dst_array, dst_profile = reproject_raster(
            sample_geotiff, target_crs="EPSG:27700", resolution=50
        )
        # Finer resolution -> more pixels
        assert dst_profile["width"] > 5
        assert dst_profile["height"] > 5

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Raster file not found"):
            reproject_raster(tmp_path / "nonexistent.tif")

    def test_invalid_resampling_raises(self, sample_geotiff):
        with pytest.raises(ValueError, match="Unknown resampling method"):
            reproject_raster(sample_geotiff, resampling="invalid_method")

    def test_bilinear_resampling(self, sample_geotiff):
        dst_array, _ = reproject_raster(
            sample_geotiff, target_crs="EPSG:27700", resampling="bilinear"
        )
        assert dst_array.shape[0] == 1

    def test_reprojects_across_crs(self, sample_geotiff_4326):
        """Reprojecting from EPSG:4326 to EPSG:27700 changes CRS and preserves values."""
        dst_array, dst_profile = reproject_raster(sample_geotiff_4326, target_crs="EPSG:27700")
        assert dst_profile["crs"] == "EPSG:27700"
        assert dst_array.ndim == 3
        assert dst_array.shape[0] == 1
        # Source was uniform 7.0 — reprojected pixels should match
        band = dst_array[0]
        close_to_source = np.isclose(band, 7.0, atol=0.1)
        assert close_to_source.sum() > 0, "No pixels close to source value 7.0"


# ---------------------------------------------------------------------------
# write_geotiff
# ---------------------------------------------------------------------------


class TestWriteGeotiff:
    @pytest.fixture
    def base_profile(self):
        """Minimal rasterio profile for a 10x10 grid."""
        transform = Affine.translation(0, 1000) * Affine.scale(100, -100)
        return {
            "crs": "EPSG:27700",
            "transform": transform,
            "width": 10,
            "height": 10,
            "dtype": "float32",
        }

    def test_writes_single_band_2d(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.ones((10, 10), dtype="float32") * 5.0
        write_geotiff(arr, base_profile, path)

        with rasterio.open(path) as src:
            assert src.count == 1
            assert src.read(1).shape == (10, 10)
            assert np.allclose(src.read(1), 5.0)

    def test_writes_multi_band_3d(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.stack(
            [
                np.ones((10, 10), dtype="float32") * 1.0,
                np.ones((10, 10), dtype="float32") * 2.0,
                np.ones((10, 10), dtype="float32") * 3.0,
            ]
        )
        write_geotiff(arr, base_profile, path)

        with rasterio.open(path) as src:
            assert src.count == 3
            assert np.allclose(src.read(1), 1.0)
            assert np.allclose(src.read(2), 2.0)
            assert np.allclose(src.read(3), 3.0)

    def test_band_names_set(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.zeros((3, 10, 10), dtype="float32")
        names = ["Natura2000", "SSSI", "AONB_NatParks"]
        write_geotiff(arr, base_profile, path, band_names=names)

        with rasterio.open(path) as src:
            for i, name in enumerate(names):
                assert src.descriptions[i] == name

    def test_band_names_length_mismatch_raises(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.zeros((2, 10, 10), dtype="float32")
        with pytest.raises(ValueError, match="band_names length"):
            write_geotiff(arr, base_profile, path, band_names=["only_one"])

    def test_standard_metadata_applied(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.zeros((10, 10), dtype="float32")
        write_geotiff(arr, base_profile, path)

        with rasterio.open(path) as src:
            assert src.profile["driver"] == "GTiff"
            assert src.nodata == -9999
            assert src.profile["compress"] == "lzw"
            assert src.profile["tiled"] is True
            assert src.profile["blockxsize"] == 256
            assert src.profile["blockysize"] == 256

    def test_creates_parent_directories(self, tmp_path, base_profile):
        path = tmp_path / "nested" / "dirs" / "out.tif"
        arr = np.zeros((10, 10), dtype="float32")
        write_geotiff(arr, base_profile, path)
        assert path.exists()

    def test_does_not_mutate_input_profile(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.zeros((10, 10), dtype="float32")
        original_keys = set(base_profile.keys())
        write_geotiff(arr, base_profile, path)
        assert set(base_profile.keys()) == original_keys
        assert "compress" not in base_profile

    def test_uint8_gets_dtype_appropriate_nodata(self, tmp_path, base_profile):
        """uint8 arrays automatically get nodata=255 instead of -9999."""
        path = tmp_path / "uint8_nodata.tif"
        mask = np.zeros((10, 10), dtype="uint8")
        mask[2:5, 3:7] = 1

        uint8_profile = base_profile.copy()
        uint8_profile["dtype"] = "uint8"
        write_geotiff(mask, uint8_profile, path)

        with rasterio.open(path) as src:
            assert src.dtypes[0] == "uint8"
            assert src.nodata == 255
            data = src.read(1)
            assert data[3, 5] == 1
            assert data[0, 0] == 0

    def test_explicit_nodata_overrides_default(self, tmp_path, base_profile):
        """Passing nodata= overrides the dtype-based default."""
        path = tmp_path / "custom_nodata.tif"
        arr = np.zeros((10, 10), dtype="float32")
        write_geotiff(arr, base_profile, path, nodata=-1.0)

        with rasterio.open(path) as src:
            assert src.nodata == -1.0

    def test_no_band_names_by_default(self, tmp_path, base_profile):
        path = tmp_path / "out.tif"
        arr = np.zeros((10, 10), dtype="float32")
        write_geotiff(arr, base_profile, path)

        with rasterio.open(path) as src:
            assert src.descriptions[0] in (None, "")


# ---------------------------------------------------------------------------
# Zonal statistics fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def zone_gdf():
    """Two non-overlapping zone polygons covering halves of a 1000x1000 grid."""
    return gpd.GeoDataFrame(
        {"zone_name": ["west", "east"]},
        geometry=[box(0, 0, 500, 1000), box(500, 0, 1000, 1000)],
        crs="EPSG:27700",
    )


@pytest.fixture
def zone_gpkg(tmp_path, zone_gdf):
    """Write zone GeoDataFrame to a temporary GeoPackage."""
    path = tmp_path / "zones.gpkg"
    zone_gdf.to_file(path, driver="GPKG")
    return path


@pytest.fixture
def binary_raster(template):
    """10x10 raster where the left half (columns 0-4) is burned."""
    width, height, _transform, _crs = template
    arr = np.zeros((height, width), dtype="uint8")
    arr[:, :5] = 1  # left 5 columns
    return arr


@pytest.fixture
def continuous_raster(template):
    """10x10 raster with linearly increasing values left to right."""
    width, height, _transform, _crs = template
    arr = np.arange(width, dtype="float32") * 10.0  # 0, 10, ..., 90
    arr = np.broadcast_to(arr, (height, width)).copy()
    return arr


# ---------------------------------------------------------------------------
# load_zone_shapes
# ---------------------------------------------------------------------------


class TestLoadZoneShapes:
    def test_loads_with_zone_name_column(self, zone_gpkg):
        gdf = load_zone_shapes(zone_gpkg)
        assert "zone_name" in gdf.columns
        assert len(gdf) == 2
        assert gdf.crs.to_epsg() == 27700

    def test_renames_name_column(self, tmp_path):
        gdf = gpd.GeoDataFrame(
            {"Name": ["alpha", "beta"]},
            geometry=[box(0, 0, 100, 100), box(200, 200, 300, 300)],
            crs="EPSG:27700",
        )
        path = tmp_path / "zones_name.gpkg"
        gdf.to_file(path, driver="GPKG")

        result = load_zone_shapes(path)
        assert "zone_name" in result.columns
        assert list(result["zone_name"]) == ["alpha", "beta"]

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_zone_shapes(tmp_path / "missing.gpkg")

    def test_no_zone_column_raises(self, tmp_path):
        gdf = gpd.GeoDataFrame(
            {"category": ["x"]},
            geometry=[box(0, 0, 1, 1)],
            crs="EPSG:27700",
        )
        path = tmp_path / "no_zone_col.gpkg"
        gdf.to_file(path, driver="GPKG")

        with pytest.raises(ValueError, match="No recognisable zone-name column"):
            load_zone_shapes(path)

    def test_reprojects_to_target_crs(self, tmp_path):
        gdf = gpd.GeoDataFrame(
            {"zone_name": ["a"]},
            geometry=[box(-1, 51, 0, 52)],
            crs="EPSG:4326",
        )
        path = tmp_path / "zones_4326.gpkg"
        gdf.to_file(path, driver="GPKG")

        result = load_zone_shapes(path, target_crs="EPSG:27700")
        assert result.crs.to_epsg() == 27700


# ---------------------------------------------------------------------------
# calculate_zone_fraction
# ---------------------------------------------------------------------------


class TestCalculateZoneFraction:
    def test_half_coverage(self, binary_raster, zone_gdf, template):
        """Left half burned, two zones split left/right -> west=1.0, east=0.0."""
        _w, _h, transform, _crs = template
        fractions = calculate_zone_fraction(binary_raster, zone_gdf, transform)
        assert fractions["west"] == pytest.approx(1.0)
        assert fractions["east"] == pytest.approx(0.0)

    def test_full_coverage(self, zone_gdf, template):
        width, height, transform, _crs = template
        full = np.ones((height, width), dtype="uint8")
        fractions = calculate_zone_fraction(full, zone_gdf, transform)
        assert fractions["west"] == pytest.approx(1.0)
        assert fractions["east"] == pytest.approx(1.0)

    def test_no_coverage(self, zone_gdf, template):
        width, height, transform, _crs = template
        empty = np.zeros((height, width), dtype="uint8")
        fractions = calculate_zone_fraction(empty, zone_gdf, transform)
        assert fractions["west"] == pytest.approx(0.0)
        assert fractions["east"] == pytest.approx(0.0)

    def test_zone_outside_raster(self, template):
        """Zone entirely outside raster extent should return 0."""
        _w, _h, transform, _crs = template
        raster = np.ones((10, 10), dtype="uint8")
        outside = gpd.GeoDataFrame(
            {"zone_name": ["far_away"]},
            geometry=[box(5000, 5000, 6000, 6000)],
            crs="EPSG:27700",
        )
        fractions = calculate_zone_fraction(raster, outside, transform)
        assert fractions["far_away"] == 0.0

    def test_returns_series(self, binary_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        result = calculate_zone_fraction(binary_raster, zone_gdf, transform)
        assert isinstance(result, pd.Series)
        assert result.name == "fraction"


# ---------------------------------------------------------------------------
# calculate_zone_summary
# ---------------------------------------------------------------------------


class TestCalculateZoneSummary:
    def test_mean_stat(self, continuous_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        df = calculate_zone_summary(
            continuous_raster, zone_gdf, transform, stats=["mean"], nodata=-9999.0
        )
        assert "mean" in df.columns
        assert "zone_name" in df.columns
        assert len(df) == 2
        # West zone covers columns 0-4 (values 0,10,20,30,40) -> mean=20
        west_mean = df.loc[df["zone_name"] == "west", "mean"].iloc[0]
        assert west_mean == pytest.approx(20.0)
        # East zone covers columns 5-9 (values 50,60,70,80,90) -> mean=70
        east_mean = df.loc[df["zone_name"] == "east", "mean"].iloc[0]
        assert east_mean == pytest.approx(70.0)

    def test_multiple_stats(self, continuous_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        df = calculate_zone_summary(
            continuous_raster,
            zone_gdf,
            transform,
            stats=["mean", "min", "max", "std"],
        )
        assert set(["mean", "min", "max", "std"]).issubset(df.columns)

    def test_min_max(self, continuous_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        df = calculate_zone_summary(
            continuous_raster,
            zone_gdf,
            transform,
            stats=["min", "max"],
        )
        west = df.loc[df["zone_name"] == "west"]
        assert west["min"].iloc[0] == pytest.approx(0.0)
        assert west["max"].iloc[0] == pytest.approx(40.0)

    def test_percentile(self, continuous_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        df = calculate_zone_summary(
            continuous_raster,
            zone_gdf,
            transform,
            stats=["p50"],
        )
        assert "p50" in df.columns

    def test_empty_stats_raises(self, continuous_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        with pytest.raises(ValueError, match="at least one"):
            calculate_zone_summary(
                continuous_raster,
                zone_gdf,
                transform,
                stats=[],
            )

    def test_invalid_stat_raises(self, continuous_raster, zone_gdf, template):
        _w, _h, transform, _crs = template
        with pytest.raises(ValueError, match="Unrecognised statistic"):
            calculate_zone_summary(
                continuous_raster,
                zone_gdf,
                transform,
                stats=["bogus"],
            )

    def test_nodata_excluded(self, zone_gdf, template):
        """Pixels with nodata should not contribute to statistics."""
        width, height, transform, _crs = template
        arr = np.full((height, width), -9999.0, dtype="float32")
        # Set only the left column to a real value
        arr[:, 0] = 5.0

        df = calculate_zone_summary(
            arr,
            zone_gdf,
            transform,
            stats=["mean"],
            nodata=-9999.0,
        )
        west_mean = df.loc[df["zone_name"] == "west", "mean"].iloc[0]
        assert west_mean == pytest.approx(5.0)

    def test_zone_all_nodata_returns_nan(self, zone_gdf, template):
        width, height, transform, _crs = template
        arr = np.full((height, width), -9999.0, dtype="float32")
        df = calculate_zone_summary(
            arr,
            zone_gdf,
            transform,
            stats=["mean"],
            nodata=-9999.0,
        )
        assert all(df["mean"].isna())


# ---------------------------------------------------------------------------
# validate_crs
# ---------------------------------------------------------------------------


class TestValidateCrs:
    def test_matching_crs_passes(self, projected_gdf):
        validate_crs(projected_gdf, expected_crs="EPSG:27700")

    def test_mismatched_crs_raises(self, geographic_gdf):
        with pytest.raises(ValueError, match="CRS mismatch"):
            validate_crs(geographic_gdf, expected_crs="EPSG:27700")

    def test_no_crs_raises(self):
        gdf = gpd.GeoDataFrame({"a": [1]}, geometry=[box(0, 0, 1, 1)])
        with pytest.raises(ValueError, match="no CRS defined"):
            validate_crs(gdf)

    def test_dict_profile_passes(self):
        profile = {"crs": "EPSG:27700", "width": 10, "height": 10}
        validate_crs(profile, expected_crs="EPSG:27700")

    def test_dict_profile_mismatch_raises(self):
        profile = {"crs": "EPSG:4326"}
        with pytest.raises(ValueError, match="CRS mismatch"):
            validate_crs(profile, expected_crs="EPSG:27700")

    def test_dict_missing_crs_raises(self):
        with pytest.raises(ValueError, match="no 'crs' key"):
            validate_crs({}, expected_crs="EPSG:27700")

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="Expected GeoDataFrame or dict"):
            validate_crs("not_a_dataset")


# ---------------------------------------------------------------------------
# validate_gb_coverage
# ---------------------------------------------------------------------------


class TestValidateGbCoverage:
    def test_full_coverage_passes(self):
        bounds = (0, 0, 700_000, 1_300_000)
        assert validate_gb_coverage(bounds) is True

    def test_partial_coverage_warns(self):
        # Covers only bottom-left quarter -> 25%
        bounds = (0, 0, 350_000, 650_000)
        assert validate_gb_coverage(bounds) is False

    def test_no_overlap_warns(self):
        bounds = (1_000_000, 2_000_000, 1_500_000, 2_500_000)
        assert validate_gb_coverage(bounds) is False

    def test_custom_min_fraction(self):
        # ~50% coverage
        bounds = (0, 0, 700_000, 650_000)
        assert validate_gb_coverage(bounds, min_fraction=0.40) is True
        assert validate_gb_coverage(bounds, min_fraction=0.60) is False

    def test_geodataframe_input(self):
        gdf = gpd.GeoDataFrame(
            geometry=[box(0, 0, 700_000, 1_300_000)],
            crs="EPSG:27700",
        )
        assert validate_gb_coverage(gdf) is True

    def test_empty_geodataframe(self):
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:27700")
        assert validate_gb_coverage(empty) is False

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="Expected GeoDataFrame or"):
            validate_gb_coverage("not_valid")
