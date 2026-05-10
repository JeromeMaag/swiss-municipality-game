"""Tests for the geo app."""

from datetime import UTC, datetime
import json
from io import StringIO
from pathlib import Path
from unittest import mock

import geopandas as gpd
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from shapely.geometry import Polygon

from tests.utils import make_test_geometry

from .management.commands.import_boundaries import simplify_geometry, to_float, to_int
from .management.commands.import_population import read_csv_rows
from .management.commands.import_statpop_population import (
    apply_population_aggregation,
    parse_statpop_population_csv,
)
from .management.commands.seed_dev_geodata import (
    DATASET_NAME,
    DATASET_VERSION,
    DEV_MUNICIPALITIES,
)
from .management.commands.import_swissboundaries3d import (
    DATASET_NAME as OFFICIAL_BOUNDARIES_DATASET_NAME,
    download_asset,
    load_stac_items,
    safe_extract_zip,
)
from .models import Canton, GeoDatasetVersion, Municipality
from .selectors import get_current_dataset_version


class GeoModelTests(TestCase):
    """Tests for geodata model behavior."""

    def setUp(self) -> None:
        """Create shared geodata fixtures for model tests."""
        self.dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
            source_url="https://example.test/source",
        )
        self.canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
            geom_simplified=make_test_geometry(),
            label_point=Point(8.05, 47.05, srid=4326),
        )

    def test_dataset_version_string(self) -> None:
        """Dataset versions expose a concise display label."""
        self.assertEqual(str(self.dataset_version), "swissBOUNDARIES3D 2026-01-01")

    def test_dataset_version_name_and_label_are_unique(self) -> None:
        """Dataset names and version labels are unique together."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeoDatasetVersion.objects.create(
                name="swissBOUNDARIES3D",
                version_label="2026-01-01",
            )

    def test_canton_string(self) -> None:
        """Cantons expose abbreviation and name in their display label."""
        self.assertEqual(str(self.canton), "ZH - Zurich")

    def test_municipality_string(self) -> None:
        """Municipalities expose name and canton abbreviation."""
        municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            population=443000,
            area_km2=87.88,
            geom=make_test_geometry(),
            geom_simplified=make_test_geometry(),
            label_point=Point(8.05, 47.05, srid=4326),
        )

        self.assertEqual(str(municipality), "Zurich (ZH)")

    def test_municipality_bfs_number_is_unique_per_dataset(self) -> None:
        """Municipality BFS numbers are unique within a dataset version."""
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Municipality.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=261,
                name="Duplicate Zurich",
                canton=self.canton,
                geom=make_test_geometry(),
            )

    def test_canton_abbreviation_is_unique_per_dataset(self) -> None:
        """Canton abbreviations are unique within a dataset version."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            Canton.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=2,
                abbreviation="ZH",
                name="Duplicate Zurich",
                geom=make_test_geometry(),
            )

    def test_canton_abbreviation_can_repeat_across_datasets(self) -> None:
        """Canton abbreviations may repeat across different dataset versions."""
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )

        canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )

        self.assertEqual(canton.abbreviation, "ZH")

    def test_municipality_bfs_number_can_repeat_across_datasets(self) -> None:
        """Municipality BFS numbers may repeat across different dataset versions."""
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )
        other_canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )

        municipality = Municipality.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        self.assertEqual(municipality.bfs_number, 261)

    def test_municipality_requires_canton_from_same_dataset(self) -> None:
        """Municipality validation rejects cantons from another dataset version."""
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )
        other_canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        municipality = Municipality(
            dataset_version=self.dataset_version,
            bfs_number=9999,
            name="Invalid Municipality",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        with self.assertRaises(ValidationError):
            municipality.full_clean()


class GeoJSONEndpointTests(TestCase):
    """Tests for geodata GeoJSON endpoints."""

    def setUp(self) -> None:
        """Create shared geodata fixtures and authenticate a user."""
        cache.clear()
        self.addCleanup(cache.clear)
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="player",
            password="StrongPass123!",
        )
        self.client.force_login(self.user)
        self.dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
        )
        self.canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
            geom_simplified=make_test_geometry(),
            label_point=Point(8.05, 47.05, srid=4326),
        )
        self.municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            population=443000,
            geom=make_test_geometry(),
            geom_simplified=make_test_geometry(),
            label_point=Point(8.05, 47.05, srid=4326),
        )

    def assert_geojson_response(self, response) -> dict:
        """Assert a successful GeoJSON response and return parsed JSON.

        Args:
            response: The Django test response to inspect.

        Returns:
            Parsed JSON response data.
        """
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/geo+json")
        return json.loads(response.content)

    def test_geojson_endpoints_require_login(self) -> None:
        """Anonymous users are redirected away from all GeoJSON endpoints."""
        self.client.logout()
        urls = [
            reverse("geo:cantons_geojson"),
            reverse("geo:municipality_boundaries_geojson"),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(
                    response["Location"].startswith(reverse("accounts:login"))
                )

    def test_canton_boundaries_returns_feature_collection(self) -> None:
        """Canton boundary endpoint returns canton properties and geometry."""
        response = self.client.get(reverse("geo:cantons_geojson"))
        data = self.assert_geojson_response(response)

        self.assertEqual(data["type"], "FeatureCollection")
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["name"], "Zurich")
        self.assertEqual(data["features"][0]["geometry"]["type"], "MultiPolygon")

    def test_municipality_boundaries_do_not_include_names(self) -> None:
        """Municipality boundary endpoint does not reveal municipality names."""
        response = self.client.get(reverse("geo:municipality_boundaries_geojson"))
        data = self.assert_geojson_response(response)

        properties = data["features"][0]["properties"]
        self.assertEqual(properties["id"], self.municipality.id)
        self.assertNotIn("bfs_number", properties)
        self.assertNotIn("name", properties)
        self.assertNotIn("canton", properties)
        self.assertNotIn("canton_abbreviation", properties)

    def test_boundary_responses_are_cacheable_per_browser(self) -> None:
        """Boundary endpoints expose private browser cache metadata."""
        response = self.client.get(reverse("geo:municipality_boundaries_geojson"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("max-age=3600", response["Cache-Control"])
        self.assertIn("ETag", response)

    def test_boundary_responses_support_conditional_gets(self) -> None:
        """Boundary endpoints return 304 when the client cache is current."""
        response = self.client.get(reverse("geo:municipality_boundaries_geojson"))
        etag = response["ETag"]

        cached_response = self.client.get(
            reverse("geo:municipality_boundaries_geojson"),
            HTTP_IF_NONE_MATCH=etag,
        )

        self.assertEqual(cached_response.status_code, 304)
        self.assertEqual(cached_response["ETag"], etag)

    def test_boundary_cache_changes_when_current_dataset_changes(self) -> None:
        """Boundary cache keys change when a newer dataset becomes current."""
        first_response = self.client.get(reverse("geo:municipality_boundaries_geojson"))
        first_etag = first_response["ETag"]
        first_data = self.assert_geojson_response(first_response)
        first_feature_id = first_data["features"][0]["properties"]["id"]
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )
        other_canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=2,
            abbreviation="BE",
            name="Bern",
            geom=make_test_geometry(),
        )
        other_municipality = Municipality.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=351,
            name="Bern",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        second_response = self.client.get(
            reverse("geo:municipality_boundaries_geojson"),
            HTTP_IF_NONE_MATCH=first_etag,
        )
        second_data = self.assert_geojson_response(second_response)

        self.assertNotEqual(second_response["ETag"], first_etag)
        self.assertNotEqual(
            second_data["features"][0]["properties"]["id"],
            first_feature_id,
        )
        self.assertEqual(
            second_data["features"][0]["properties"]["id"],
            other_municipality.id,
        )

    def test_municipality_boundaries_are_not_ordered_by_name(self) -> None:
        """Municipality boundary endpoint avoids name-based feature ordering."""
        other_municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            name="Aarau",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        response = self.client.get(reverse("geo:municipality_boundaries_geojson"))
        data = self.assert_geojson_response(response)

        feature_ids = [feature["properties"]["id"] for feature in data["features"]]
        self.assertEqual(feature_ids, [self.municipality.id, other_municipality.id])

    def test_feature_collection_endpoints_are_empty_without_dataset(self) -> None:
        """Feature collection endpoints return empty data before import."""
        Municipality.objects.all().delete()
        Canton.objects.all().delete()
        GeoDatasetVersion.objects.all().delete()
        urls = [
            reverse("geo:cantons_geojson"),
            reverse("geo:municipality_boundaries_geojson"),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                data = self.assert_geojson_response(response)
                self.assertEqual(data, {"type": "FeatureCollection", "features": []})


class ImportBoundariesCommandTests(TestCase):
    """Tests for the boundary import management command."""

    def test_numeric_conversion_accepts_common_gis_values(self) -> None:
        """Import conversion helpers accept numeric strings from GIS files."""
        self.assertEqual(to_int("261.0"), 261)
        self.assertIsNone(to_int(""))
        self.assertEqual(to_float("87.88"), 87.88)
        self.assertIsNone(to_float(""))

    def test_numeric_conversion_rejects_invalid_values(self) -> None:
        """Import conversion helpers reject ambiguous numeric values."""
        with self.assertRaises(CommandError):
            to_int("261.5")

        with self.assertRaises(CommandError):
            to_float("not-a-number")

        with self.assertRaises(CommandError):
            to_float("nan")

    def test_simplify_geometry_returns_none_without_tolerance(self) -> None:
        """Geometry simplification does not duplicate full geometries by default."""
        self.assertIsNone(simplify_geometry(make_test_geometry(), 0.0))

    def test_simplify_geometry_returns_geometry_with_tolerance(self) -> None:
        """Geometry simplification stores a geometry when explicitly enabled."""
        simplified = simplify_geometry(make_test_geometry(), 0.0001)

        self.assertIsNotNone(simplified)
        self.assertEqual(simplified.srid, 4326)

    def test_command_lists_layers_when_layer_names_are_missing(self) -> None:
        """Command lists datasource layers when no layer names are provided."""
        output = StringIO()
        source = Path("boundaries.gpkg")
        with mock.patch(
            "geo.management.commands.import_boundaries.resolve_data_source",
            return_value=source,
        ):
            with mock.patch("pyogrio.list_layers") as list_layers:
                list_layers.return_value = [
                    ("municipalities", "MultiPolygon"),
                    ("cantons", "MultiPolygon"),
                ]

                call_command(
                    "import_boundaries",
                    str(source),
                    "--dataset-version",
                    "2026-01-01",
                    stdout=output,
                )

        self.assertIn("Available layers:", output.getvalue())
        self.assertIn("municipalities", output.getvalue())
        self.assertEqual(GeoDatasetVersion.objects.count(), 0)

    def test_command_imports_cantons_and_municipalities(self) -> None:
        """Command imports boundaries and can rerun without duplicates."""
        output = StringIO()
        canton_gdf = gpd.GeoDataFrame(
            [
                {
                    "BFS_NUMMER": "1.0",
                    "KANTON": "ZH",
                    "NAME": "Zurich",
                    "geometry": Polygon(
                        (
                            (8.0, 47.0),
                            (8.2, 47.0),
                            (8.2, 47.2),
                            (8.0, 47.2),
                            (8.0, 47.0),
                        )
                    ),
                }
            ],
            crs="EPSG:4326",
        )
        municipality_gdf = gpd.GeoDataFrame(
            [
                {
                    "BFS_NUMMER": "261.0",
                    "KANTON": "ZH",
                    "NAME": "Zurich",
                    "AREA_KM2": "87.88",
                    "geometry": Polygon(
                        (
                            (8.02, 47.02),
                            (8.08, 47.02),
                            (8.08, 47.08),
                            (8.02, 47.08),
                            (8.02, 47.02),
                        )
                    ),
                }
            ],
            crs="EPSG:4326",
        )

        def read_layer(_source, layer):
            """Return a fake GeoDataFrame for a layer.

            Args:
                _source: Ignored datasource path.
                layer: Requested layer name.

            Returns:
                The matching fake GeoDataFrame.
            """
            return canton_gdf if layer == "cantons" else municipality_gdf

        source = Path("boundaries.gpkg")
        old_imported_at = datetime(2026, 1, 1, tzinfo=UTC)
        with mock.patch(
            "geo.management.commands.import_boundaries.resolve_data_source",
            return_value=source,
        ):
            with mock.patch(
                "geo.management.commands.import_boundaries.read_layer",
                side_effect=read_layer,
            ):
                call_command(
                    "import_boundaries",
                    str(source),
                    "--dataset-version",
                    "2026-01-01",
                    "--canton-layer",
                    "cantons",
                    "--municipality-layer",
                    "municipalities",
                    "--municipality-area-field",
                    "AREA_KM2",
                    stdout=output,
                )
                GeoDatasetVersion.objects.update(imported_at=old_imported_at)
                call_command(
                    "import_boundaries",
                    str(source),
                    "--dataset-version",
                    "2026-01-01",
                    "--canton-layer",
                    "cantons",
                    "--municipality-layer",
                    "municipalities",
                    "--municipality-area-field",
                    "AREA_KM2",
                    stdout=output,
                )

        dataset_version = GeoDatasetVersion.objects.get()
        canton = Canton.objects.get()
        municipality = Municipality.objects.get()

        self.assertEqual(str(dataset_version), "swissBOUNDARIES3D 2026-01-01")
        self.assertGreater(dataset_version.imported_at, old_imported_at)
        self.assertEqual(canton.abbreviation, "ZH")
        self.assertEqual(canton.geom.srid, 4326)
        self.assertIsNone(canton.geom_simplified)
        self.assertEqual(municipality.bfs_number, 261)
        self.assertEqual(municipality.canton, canton)
        self.assertEqual(municipality.area_km2, 87.88)
        self.assertIsNone(municipality.geom_simplified)
        self.assertIsNotNone(municipality.label_point)
        self.assertEqual(Canton.objects.count(), 1)
        self.assertEqual(Municipality.objects.count(), 1)


class ImportSwissBoundaries3DCommandTests(TestCase):
    """Tests for the official swissBOUNDARIES3D import command."""

    class RedirectResponse:
        """Minimal context manager for mocked URL responses."""

        def __init__(self, url: str, content: bytes = b"{}") -> None:
            """Store mocked response data.

            Args:
                url: Final response URL.
                content: Response body bytes.
            """
            self.url = url
            self.content = content

        def __enter__(self):
            """Return the mocked response object.

            Returns:
                The response object.
            """
            return self

        def __exit__(self, *_args) -> None:
            """Close the mocked response context."""

        def geturl(self) -> str:
            """Return the final response URL.

            Returns:
                The mocked final URL.
            """
            return self.url

        def read(self) -> bytes:
            """Read mocked response content.

            Returns:
                Response content bytes.
            """
            return self.content

    def test_command_rejects_unsupported_stac_url_scheme(self) -> None:
        """Command rejects non-HTTP STAC item URLs."""
        with self.assertRaisesMessage(
            CommandError,
            "URL scheme 'file' is not allowed.",
        ):
            call_command(
                "import_swissboundaries3d",
                "--stac-items-url",
                "file:///tmp/items.json",
                stdout=StringIO(),
            )

    def test_load_stac_items_rejects_unsupported_redirect_scheme(self) -> None:
        """STAC requests reject redirects to non-HTTP URLs."""
        response = self.RedirectResponse("file:///tmp/items.json")
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesMessage(
                CommandError,
                "URL scheme 'file' is not allowed.",
            ):
                load_stac_items("https://example.test/items.json")

    def test_download_asset_rejects_unsupported_url_scheme(self) -> None:
        """Asset downloads reject non-HTTP URLs."""
        with self.assertRaisesMessage(
            CommandError,
            "URL scheme 'file' is not allowed.",
        ):
            download_asset("file:///tmp/boundaries.gpkg.zip", Path("asset.zip"))

    def test_download_asset_rejects_unsupported_redirect_scheme(self) -> None:
        """Asset downloads reject redirects to non-HTTP URLs."""
        response = self.RedirectResponse("file:///tmp/boundaries.gpkg.zip")
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesMessage(
                CommandError,
                "URL scheme 'file' is not allowed.",
            ):
                download_asset("https://example.test/boundaries.gpkg.zip", Path("asset.zip"))

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        """ZIP extraction rejects archive members outside the destination."""
        archive = mock.Mock()
        member = mock.Mock()
        member.filename = "../outside.gpkg"
        member.external_attr = 0
        member.is_dir.return_value = False
        archive.infolist.return_value = [member]

        with self.assertRaises(CommandError):
            safe_extract_zip(archive, Path("data/raw/import-test"))

        archive.extractall.assert_not_called()

    def test_safe_extract_zip_rejects_symlinks(self) -> None:
        """ZIP extraction rejects Unix symlink entries."""
        archive = mock.Mock()
        member = mock.Mock()
        member.filename = "link.gpkg"
        member.external_attr = 0o120000 << 16
        member.is_dir.return_value = False
        archive.infolist.return_value = [member]

        with self.assertRaisesMessage(
            CommandError,
            "Unsafe symlink found in swissBOUNDARIES3D ZIP.",
        ):
            safe_extract_zip(archive, Path("data/raw/import-test"))

        archive.open.assert_not_called()

    def test_command_imports_latest_geopackage_asset(self) -> None:
        """Command downloads the newest official GeoPackage and imports boundaries."""
        output = StringIO()
        asset_url = "https://example.test/swissboundaries3d_2026-01.gpkg.zip"
        stac_items = {
            "features": [
                {
                    "id": "swissboundaries3d_2025-01",
                    "properties": {"datetime": "2025-01-01T00:00:00Z"},
                    "assets": {
                        "old.gpkg.zip": {
                            "href": "https://example.test/old.gpkg.zip",
                            "type": "application/x.geopackage+zip",
                        },
                    },
                },
                {
                    "id": "swissboundaries3d_2026-01",
                    "properties": {"datetime": "2026-01-01T00:00:00Z"},
                    "assets": {
                        "current.gpkg.zip": {
                            "href": asset_url,
                            "type": "application/x.geopackage+zip",
                        },
                    },
                },
            ],
        }
        canton_gdf = gpd.GeoDataFrame(
            [
                {
                    "kantonsnummer": 1,
                    "name": "Zurich",
                    "geometry": Polygon(
                        (
                            (8.0, 47.0),
                            (8.2, 47.0),
                            (8.2, 47.2),
                            (8.0, 47.2),
                            (8.0, 47.0),
                        )
                    ),
                },
            ],
            crs="EPSG:4326",
        )
        municipality_gdf = gpd.GeoDataFrame(
            [
                {
                    "objektart": "Gemeindegebiet",
                    "bfs_nummer": 131,
                    "kantonsnummer": 1,
                    "name": "Adliswil",
                    "geometry": Polygon(
                        (
                            (8.02, 47.02),
                            (8.08, 47.02),
                            (8.08, 47.08),
                            (8.02, 47.08),
                            (8.02, 47.02),
                        )
                    ),
                },
                {
                    "objektart": "Kantonsgebiet",
                    "bfs_nummer": 9051,
                    "kantonsnummer": 1,
                    "name": "Zurichsee (ZH)",
                    "geometry": Polygon(
                        (
                            (8.1, 47.1),
                            (8.15, 47.1),
                            (8.15, 47.15),
                            (8.1, 47.15),
                            (8.1, 47.1),
                        )
                    ),
                },
                {
                    "objektart": "Gemeindegebiet",
                    "bfs_nummer": 7004,
                    "kantonsnummer": None,
                    "name": "Triesenberg",
                    "geometry": Polygon(
                        (
                            (9.5, 47.0),
                            (9.6, 47.0),
                            (9.6, 47.1),
                            (9.5, 47.1),
                            (9.5, 47.0),
                        )
                    ),
                },
            ],
            crs="EPSG:4326",
        )

        def read_official_layer(_source, layer):
            """Return fake swissBOUNDARIES3D layers.

            Args:
                _source: Ignored GeoPackage path.
                layer: Requested layer name.

            Returns:
                The matching fake GeoDataFrame.
            """
            if layer == "tlm_kantonsgebiet":
                return canton_gdf
            if layer == "tlm_hoheitsgebiet":
                return municipality_gdf
            raise AssertionError(f"Unexpected layer requested: {layer}")

        with (
            mock.patch(
                "geo.management.commands.import_swissboundaries3d.load_stac_items",
                return_value=stac_items,
            ),
            mock.patch(
                "geo.management.commands.import_swissboundaries3d.download_asset",
            ) as download_asset,
            mock.patch(
                "geo.management.commands.import_swissboundaries3d."
                "extract_single_geopackage",
                return_value=Path("official.gpkg"),
            ),
            mock.patch(
                "geo.management.commands.import_swissboundaries3d.read_layer",
                side_effect=read_official_layer,
            ),
        ):
            call_command("import_swissboundaries3d", stdout=output)

        dataset_version = GeoDatasetVersion.objects.get(
            name=OFFICIAL_BOUNDARIES_DATASET_NAME,
            version_label="2026-01-01",
        )
        canton = Canton.objects.get()
        municipality = Municipality.objects.get()

        download_asset.assert_called_once_with(asset_url, mock.ANY)
        self.assertEqual(dataset_version.source_url, asset_url)
        self.assertEqual(canton.abbreviation, "ZH")
        self.assertEqual(canton.bfs_number, 1)
        self.assertEqual(municipality.name, "Adliswil")
        self.assertEqual(municipality.bfs_number, 131)
        self.assertEqual(municipality.canton, canton)
        self.assertEqual(Municipality.objects.count(), 1)
        self.assertIn("Imported 1 cantons and 1 municipalities", output.getvalue())


class ImportPopulationCommandTests(TestCase):
    """Tests for the population import management command."""

    def setUp(self) -> None:
        """Create shared geodata fixtures for population import tests."""
        self.dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
        )
        self.canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        self.municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )

    def set_old_municipality_updated_at(self) -> datetime:
        """Set the fixture municipality to an old update timestamp.

        Returns:
            The timestamp written to the database.
        """
        old_updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        Municipality.objects.filter(id=self.municipality.id).update(
            updated_at=old_updated_at
        )
        return old_updated_at

    def test_read_csv_rows_reads_header_and_rows(self) -> None:
        """CSV reader returns field names and row dictionaries."""
        csv_content = "bfs_number;population\n261;443000\n"
        with mock.patch("pathlib.Path.open", mock.mock_open(read_data=csv_content)):
            fieldnames, rows = read_csv_rows(Path("population.csv"), ";")

        self.assertEqual(fieldnames, ["bfs_number", "population"])
        self.assertEqual(rows, [{"bfs_number": "261", "population": "443000"}])

    def test_command_updates_current_dataset_population(self) -> None:
        """Command updates populations on the current dataset version."""
        output = StringIO()
        old_updated_at = self.set_old_municipality_updated_at()
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs_number", "population"],
                    [
                        {"bfs_number": "261", "population": "443000"},
                        {"bfs_number": "9999", "population": "123"},
                    ],
                ),
            ):
                call_command("import_population", "population.csv", stdout=output)

        self.municipality.refresh_from_db()
        self.assertEqual(self.municipality.population, 443000)
        self.assertGreater(self.municipality.updated_at, old_updated_at)
        self.assertIn("Updated 1 municipalities", output.getvalue())
        self.assertIn("Missing municipalities for BFS numbers: 9999", output.getvalue())

    def test_command_can_target_explicit_dataset_version(self) -> None:
        """Command updates the explicitly selected dataset version only."""
        other_dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2027-01-01",
        )
        other_canton = Canton.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        other_municipality = Municipality.objects.create(
            dataset_version=other_dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=other_canton,
            population=1,
            geom=make_test_geometry(),
        )

        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs_number", "population"],
                    [{"bfs_number": "261", "population": "443000"}],
                ),
            ):
                call_command(
                    "import_population",
                    "population.csv",
                    "--dataset-version",
                    "2026-01-01",
                    stdout=StringIO(),
                )

        self.municipality.refresh_from_db()
        other_municipality.refresh_from_db()
        self.assertEqual(self.municipality.population, 443000)
        self.assertEqual(other_municipality.population, 1)

    def test_command_supports_custom_columns_and_delimiter(self) -> None:
        """Command imports configurable CSV columns and delimiters."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs", "pop"],
                    [{"bfs": "261.0", "pop": "443000.0"}],
                ),
            ) as read_csv_rows:
                call_command(
                    "import_population",
                    "population.csv",
                    "--bfs-column",
                    "bfs",
                    "--population-column",
                    "pop",
                    "--delimiter",
                    ";",
                    stdout=StringIO(),
                )

        self.municipality.refresh_from_db()
        self.assertEqual(self.municipality.population, 443000)
        read_csv_rows.assert_called_once_with(Path("population.csv"), ";")

    def test_command_rejects_invalid_population_values(self) -> None:
        """Command rejects non-numeric population values."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs_number", "population"],
                    [{"bfs_number": "261", "population": "unknown"}],
                ),
            ):
                with self.assertRaises(CommandError):
                    call_command("import_population", "population.csv", stdout=StringIO())

    def test_command_rejects_negative_population_values(self) -> None:
        """Command rejects negative population values using the configured column."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs_number", "pop"],
                    [{"bfs_number": "261", "pop": "-1"}],
                ),
            ):
                with self.assertRaisesMessage(
                    CommandError,
                    "Row 2: pop must not be negative.",
                ):
                    call_command(
                        "import_population",
                        "population.csv",
                        "--population-column",
                        "pop",
                        stdout=StringIO(),
                    )

    def test_command_rejects_duplicate_bfs_rows(self) -> None:
        """Command rejects duplicate BFS numbers in one CSV import."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs_number", "population"],
                    [
                        {"bfs_number": "261", "population": "443000"},
                        {"bfs_number": "261", "population": "443001"},
                    ],
                ),
            ):
                with self.assertRaisesMessage(
                    CommandError,
                    "Row 3: duplicate BFS number 261.",
                ):
                    call_command("import_population", "population.csv", stdout=StringIO())

    def test_command_rejects_missing_columns(self) -> None:
        """Command rejects CSV files without required columns."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(["bfs_number"], [{"bfs_number": "261"}]),
            ):
                with self.assertRaises(CommandError):
                    call_command("import_population", "population.csv", stdout=StringIO())

    def test_command_rejects_duplicate_csv_columns(self) -> None:
        """Command rejects duplicate CSV header names."""
        csv_content = "bfs_number,bfs_number,population\n261,9999,443000\n"
        with mock.patch("pathlib.Path.open", mock.mock_open(read_data=csv_content)):
            with self.assertRaises(CommandError):
                read_csv_rows(Path("population.csv"), ",")

    def test_command_rejects_reused_configured_columns(self) -> None:
        """Command rejects using one CSV column for BFS and population."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(["value"], [{"value": "261"}]),
            ):
                with self.assertRaises(CommandError):
                    call_command(
                        "import_population",
                        "population.csv",
                        "--bfs-column",
                        "value",
                        "--population-column",
                        "value",
                        stdout=StringIO(),
                    )

    def test_command_rejects_non_positive_bfs_numbers(self) -> None:
        """Command rejects zero or negative BFS numbers."""
        with mock.patch(
            "geo.management.commands.import_population.resolve_csv_path",
            return_value=Path("population.csv"),
        ):
            with mock.patch(
                "geo.management.commands.import_population.read_csv_rows",
                return_value=(
                    ["bfs_number", "population"],
                    [{"bfs_number": "0", "population": "443000"}],
                ),
            ):
                with self.assertRaises(CommandError):
                    call_command("import_population", "population.csv", stdout=StringIO())


class ImportStatpopPopulationCommandTests(TestCase):
    """Tests for the official BFS STATPOP population import command."""

    def setUp(self) -> None:
        """Create shared geodata fixtures for STATPOP import tests."""
        self.dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
        )
        self.canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )

    def create_municipality(self, bfs_number: int, name: str) -> Municipality:
        """Create one municipality in the shared dataset version.

        Args:
            bfs_number: Municipality BFS number.
            name: Municipality display name.

        Returns:
            Created municipality.
        """
        return Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=bfs_number,
            name=name,
            canton=self.canton,
            geom=make_test_geometry(),
        )

    def test_parse_statpop_population_csv_extracts_municipality_rows(self) -> None:
        """STATPOP parser extracts municipality rows and skips aggregate rows."""
        statpop_csv = "\n".join(
            [
                (
                    '"Year","Canton (-) / District (>>) / Commune (......)",'
                    '"Population type","Domicile 1 year ago","Sex","Age - total"'
                ),
                (
                    '"2024","Switzerland","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",9051029'
                ),
                (
                    '"2024","- Zurich","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",1620020'
                ),
                (
                    '"2024","......0261 Zurich","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",443000'
                ),
            ]
        )

        population_by_bfs = parse_statpop_population_csv(statpop_csv)

        self.assertEqual(population_by_bfs, {261: 443000})

    def test_apply_population_aggregation_adds_successor_municipalities(self) -> None:
        """Known municipality mutations are aggregated onto successor BFS numbers."""
        population_by_bfs = apply_population_aggregation(
            {
                2016: 550,
                2027: 650,
                5146: 100,
                5149: 200,
                5181: 300,
                5200: 400,
                5207: 500,
            }
        )

        self.assertEqual(population_by_bfs[2056], 1200)
        self.assertEqual(population_by_bfs[5395], 1500)
        self.assertEqual(apply_population_aggregation({1065: 6500})[1065], 6500)

    def test_command_imports_latest_statpop_population(self) -> None:
        """Command imports the latest STATPOP data into the target dataset."""
        zurich = self.create_municipality(261, "Zurich")
        root = self.create_municipality(1065, "Root")
        fetigny_menieres = self.create_municipality(2056, "Fetigny-Menieres")
        statpop_csv = "\n".join(
            [
                (
                    '"Year","Canton (-) / District (>>) / Commune (......)",'
                    '"Population type","Domicile 1 year ago","Sex","Age - total"'
                ),
                (
                    '"2024","......0261 Zurich","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",443000'
                ),
                (
                    '"2024","......1065 Root","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",6000'
                ),
                (
                    '"2024","......1057 Honau","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",500'
                ),
                (
                    '"2024","......2016 Fetigny","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",700'
                ),
                (
                    '"2024","......2027 Menieres","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",800'
                ),
            ]
        )
        output = StringIO()

        with (
            mock.patch(
                "geo.management.commands.import_statpop_population."
                "fetch_statpop_metadata",
                return_value={
                    "variables": [{"code": "Jahr", "values": ["2023", "2024"]}]
                },
            ),
            mock.patch(
                "geo.management.commands.import_statpop_population.fetch_statpop_csv",
                return_value=statpop_csv,
            ) as fetch_statpop_csv,
        ):
            call_command(
                "import_statpop_population",
                "--dataset-version",
                "2026-01-01",
                stdout=output,
            )

        zurich.refresh_from_db()
        root.refresh_from_db()
        fetigny_menieres.refresh_from_db()
        self.assertEqual(zurich.population, 443000)
        self.assertEqual(root.population, 6500)
        self.assertEqual(fetigny_menieres.population, 1500)
        fetch_statpop_csv.assert_called_once_with(mock.ANY, "2024")
        self.assertIn("Updated 3 municipalities", output.getvalue())
        self.assertIn("BFS STATPOP 2024", output.getvalue())

    def test_command_rejects_missing_current_municipality_population(self) -> None:
        """Command fails when current municipalities have no STATPOP value."""
        self.create_municipality(9999, "Missing Municipality")
        statpop_csv = "\n".join(
            [
                (
                    '"Year","Canton (-) / District (>>) / Commune (......)",'
                    '"Population type","Domicile 1 year ago","Sex","Age - total"'
                ),
                (
                    '"2024","......0261 Zurich","Permanent resident population",'
                    '"Domicile 1 year ago - total","Sex - total",443000'
                ),
            ]
        )

        with (
            mock.patch(
                "geo.management.commands.import_statpop_population."
                "fetch_statpop_metadata",
                return_value={"variables": [{"code": "Jahr", "values": ["2024"]}]},
            ),
            mock.patch(
                "geo.management.commands.import_statpop_population.fetch_statpop_csv",
                return_value=statpop_csv,
            ),
        ):
            with self.assertRaisesMessage(
                CommandError,
                "Missing STATPOP population values",
            ):
                call_command(
                    "import_statpop_population",
                    "--dataset-version",
                    "2026-01-01",
                    stdout=StringIO(),
                )


class SetupGeodataCommandTests(TestCase):
    """Tests for the complete official geodata setup command."""

    def create_imported_dataset(
        self,
        *,
        municipality_population: int | None = None,
    ) -> Municipality:
        """Create an official dataset as if the boundary import had run.

        Args:
            municipality_population: Optional population value for the municipality.

        Returns:
            Created municipality.
        """
        dataset_version = GeoDatasetVersion.objects.create(
            name=OFFICIAL_BOUNDARIES_DATASET_NAME,
            version_label="2026-01-01",
        )
        canton = Canton.objects.create(
            dataset_version=dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        return Municipality.objects.create(
            dataset_version=dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=canton,
            population=municipality_population,
            geom=make_test_geometry(),
        )

    def test_command_imports_boundaries_then_population(self) -> None:
        """Setup command imports boundaries, imports population, and validates output."""
        output = StringIO()
        command_names = []

        def run_inner_command(command_name, *args, **kwargs):
            """Simulate the setup command's inner imports.

            Args:
                command_name: Name of the inner management command.
                *args: Positional command arguments.
                **kwargs: Keyword command options.
            """
            command_names.append(command_name)
            if command_name == "import_swissboundaries3d":
                self.create_imported_dataset()
            elif command_name == "import_statpop_population":
                Municipality.objects.update(population=443000)

        with mock.patch(
            "geo.management.commands.setup_geodata.call_command",
            side_effect=run_inner_command,
        ) as inner_call_command:
            call_command(
                "setup_geodata",
                "--dataset-version",
                "2026-01-01",
                "--statpop-year",
                "2024",
                stdout=output,
            )

        self.assertEqual(
            command_names,
            ["import_swissboundaries3d", "import_statpop_population"],
        )
        boundary_call = inner_call_command.call_args_list[0]
        population_call = inner_call_command.call_args_list[1]
        self.assertEqual(boundary_call.kwargs["dataset_version"], "2026-01-01")
        self.assertEqual(population_call.kwargs["dataset_version"], "2026-01-01")
        self.assertEqual(population_call.kwargs["year"], "2024")
        self.assertIn("Geodata setup complete", output.getvalue())
        self.assertIn("1 municipalities", output.getvalue())

    def test_command_rejects_missing_population_after_setup(self) -> None:
        """Setup command fails when imported municipalities still lack population."""
        def run_inner_command(command_name, *args, **kwargs):
            """Simulate imports while leaving population empty.

            Args:
                command_name: Name of the inner management command.
                *args: Positional command arguments.
                **kwargs: Keyword command options.
            """
            if command_name == "import_swissboundaries3d":
                self.create_imported_dataset()

        with mock.patch(
            "geo.management.commands.setup_geodata.call_command",
            side_effect=run_inner_command,
        ):
            with self.assertRaisesMessage(
                CommandError,
                "without population values",
            ):
                call_command(
                    "setup_geodata",
                    "--dataset-version",
                    "2026-01-01",
                    stdout=StringIO(),
                )

    def test_command_can_allow_incomplete_population(self) -> None:
        """Setup command can finish with explicit incomplete-population allowance."""
        output = StringIO()

        def run_inner_command(command_name, *args, **kwargs):
            """Simulate imports while leaving population empty.

            Args:
                command_name: Name of the inner management command.
                *args: Positional command arguments.
                **kwargs: Keyword command options.
            """
            if command_name == "import_swissboundaries3d":
                self.create_imported_dataset()

        with mock.patch(
            "geo.management.commands.setup_geodata.call_command",
            side_effect=run_inner_command,
        ):
            call_command(
                "setup_geodata",
                "--dataset-version",
                "2026-01-01",
                "--allow-incomplete-population",
                stdout=output,
            )

        self.assertIn("1 municipalities, 1 without population", output.getvalue())


class SeedDevGeodataCommandTests(TestCase):
    """Tests for local development geodata seeding."""

    def test_command_creates_dev_dataset_with_active_municipalities(self) -> None:
        """Seed command creates a current dataset with five active municipalities."""
        output = StringIO()

        call_command("seed_dev_geodata", stdout=output)

        dataset_version = GeoDatasetVersion.objects.get(
            name=DATASET_NAME,
            version_label=DATASET_VERSION,
        )
        self.assertEqual(
            dataset_version.notes,
            "Local dummy geodata for development only.",
        )
        self.assertEqual(dataset_version.cantons.count(), 1)
        self.assertEqual(
            dataset_version.municipalities.filter(is_active=True).count(),
            len(DEV_MUNICIPALITIES),
        )
        self.assertIn(
            f"Seeded {len(DEV_MUNICIPALITIES)} development municipalities.",
            output.getvalue(),
        )

    def test_command_is_idempotent(self) -> None:
        """Running the seed command twice does not duplicate records."""
        call_command("seed_dev_geodata", stdout=StringIO())
        call_command("seed_dev_geodata", stdout=StringIO())

        dataset_version = GeoDatasetVersion.objects.get(
            name=DATASET_NAME,
            version_label=DATASET_VERSION,
        )
        self.assertEqual(dataset_version.cantons.count(), 1)
        self.assertEqual(
            dataset_version.municipalities.count(),
            len(DEV_MUNICIPALITIES),
        )

    def test_command_refreshes_current_dataset_timestamp(self) -> None:
        """Seed command makes the dev dataset the current dataset."""
        call_command("seed_dev_geodata", stdout=StringIO())
        GeoDatasetVersion.objects.create(
            name="newer-dataset",
            version_label="local",
        )

        call_command("seed_dev_geodata", stdout=StringIO())

        self.assertEqual(get_current_dataset_version().name, DATASET_NAME)
