"""Tests for the geo app."""

from datetime import UTC, datetime
import json
from io import StringIO
from pathlib import Path
from unittest import mock

import geopandas as gpd
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from shapely.geometry import Polygon

from tests.utils import make_test_geometry

from .management.commands.import_boundaries import simplify_geometry, to_float, to_int
from .models import Canton, GeoDatasetVersion, Municipality


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
