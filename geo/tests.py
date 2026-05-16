"""Tests for the geo app."""

from datetime import UTC, datetime, timedelta
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request
import zipfile

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
from django.utils import timezone
from shapely.geometry import Polygon

from geo.admin_views import truncate_command_output
from geo.constants import MUNICIPALITY_LABEL_ACCESS_SESSION_KEY
from game.identity import GUEST_PLAYER_SESSION_KEY
from game.models import Game, Turn
from tests.utils import make_test_geometry

from .management.commands._http import (
    build_redirect_request,
    open_url_with_validated_redirects,
)
from .management.commands.import_boundaries import (
    list_available_layers,
    resolve_data_source,
    simplify_geometry,
    to_float,
    to_int,
)
from .management.commands.import_population import (
    format_bfs_numbers,
    parse_whole_number,
    read_csv_rows,
    resolve_csv_path,
    resolve_dataset_version,
)
from .management.commands.import_statpop_population import (
    apply_population_aggregation,
    build_statpop_query,
    decode_statpop_response,
    fetch_statpop_csv,
    fetch_statpop_metadata,
    metadata_variable,
    parse_municipality_bfs_number,
    parse_population_value,
    parse_statpop_population_csv,
    population_rows_for_dataset,
    select_statpop_year,
)
from .management.commands.setup_geodata import (
    resolve_setup_dataset_version,
    validate_setup_result,
)
from .management.commands.seed_dev_geodata import (
    DATASET_NAME,
    DATASET_VERSION,
    DEV_MUNICIPALITIES,
)
from .management.commands.import_swissboundaries3d import (
    DATASET_NAME as OFFICIAL_BOUNDARIES_DATASET_NAME,
    dataset_version_label,
    download_asset,
    extract_single_geopackage,
    load_stac_items,
    safe_extract_zip,
    select_geopackage_asset,
    select_stac_item,
)
from .management.commands.import_villages import (
    download_asset as download_village_asset,
    import_villages,
    resolve_layer_source as resolve_village_layer_source,
    safe_extract_zip as safe_extract_village_zip,
)
from .models import Canton, GeoDatasetVersion, Municipality, Village
from .serializers import feature_collection, get_display_geometry
from .selectors import (
    get_cantons_for_dataset,
    get_current_cantons,
    get_current_dataset_version,
    get_current_municipalities,
    get_municipality_labels_for_dataset,
    get_municipalities_for_dataset,
)


def make_redirect_error(url: str, location: str) -> HTTPError:
    """Build a mocked HTTP redirect error.

    Args:
        url: URL that returned the redirect.
        location: Redirect Location header value.

    Returns:
        HTTPError representing a redirect response.
    """
    return HTTPError(url, 302, "Found", {"Location": location}, None)


class ClosingRedirectError(HTTPError):
    """HTTP redirect error that records whether it was closed."""

    def __init__(self, url: str, location: str) -> None:
        """Create a close-tracking redirect error.

        Args:
            url: URL that returned the redirect.
            location: Redirect Location header value.
        """
        super().__init__(url, 302, "Found", {"Location": location}, None)
        self.closed = False

    def close(self) -> None:
        """Record that the error response was closed."""
        self.closed = True
        super().close()


class GeoHttpHelperTests(TestCase):
    """Tests for shared geodata HTTP helpers."""

    def test_common_redirect_switches_post_to_get_without_body_headers(self) -> None:
        """301, 302, and 303 redirects drop POST bodies before refetching."""
        original_request = Request(
            "https://www.pxweb.bfs.admin.ch/table.px",
            data=b'{"query":[]}',
            headers={"Accept": "text/csv", "Content-Type": "application/json"},
            method="POST",
        )

        redirect_request = build_redirect_request(
            original_request,
            "https://www.pxweb.bfs.admin.ch/redirected.px",
            303,
        )

        self.assertEqual(redirect_request.get_method(), "GET")
        self.assertIsNone(redirect_request.data)
        self.assertEqual(dict(redirect_request.header_items())["Accept"], "text/csv")
        self.assertNotIn("Content-type", dict(redirect_request.header_items()))

    def test_temporary_redirect_preserves_method_and_body(self) -> None:
        """307 and 308 redirects preserve the original request semantics."""
        original_request = Request(
            "https://www.pxweb.bfs.admin.ch/table.px",
            data=b'{"query":[]}',
            headers={"Accept": "text/csv", "Content-Type": "application/json"},
            method="POST",
        )

        redirect_request = build_redirect_request(
            original_request,
            "https://www.pxweb.bfs.admin.ch/redirected.px",
            307,
        )

        self.assertEqual(redirect_request.get_method(), "POST")
        self.assertEqual(redirect_request.data, b'{"query":[]}')
        self.assertEqual(
            dict(redirect_request.header_items())["Content-type"],
            "application/json",
        )

    def test_redirect_errors_are_closed_before_following(self) -> None:
        """Redirect response handles are closed before following Location."""
        redirect_error = ClosingRedirectError(
            "https://data.geo.admin.ch/items.json",
            "https://data.geo.admin.ch/redirected.json",
        )
        response = mock.Mock()
        response.geturl.return_value = "https://data.geo.admin.ch/redirected.json"
        opener = mock.Mock()
        opener.open.side_effect = [redirect_error, response]

        with mock.patch("urllib.request.build_opener", return_value=opener):
            self.assertIs(
                open_url_with_validated_redirects(
                    Request("https://data.geo.admin.ch/items.json"),
                    timeout=60,
                    validate_url=lambda _url: None,
                ),
                response,
            )

        self.assertTrue(redirect_error.closed)


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

    def test_village_string(self) -> None:
        """Villages expose name, postal code, and canton abbreviation."""
        village = Village.objects.create(
            dataset_version=self.dataset_version,
            source_identifier="village-1",
            name="Aadorf",
            postal_code="8355",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        self.assertEqual(str(village), "Aadorf 8355 (ZH)")

    def test_village_string_without_postal_code(self) -> None:
        """Villages can be displayed when the source has no postal code."""
        village = Village.objects.create(
            dataset_version=self.dataset_version,
            name="Aadorf",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        self.assertEqual(str(village), "Aadorf (ZH)")

    def test_village_source_identifier_is_unique_per_dataset(self) -> None:
        """Non-empty village source identifiers are unique per dataset."""
        Village.objects.create(
            dataset_version=self.dataset_version,
            source_identifier="village-1",
            name="Aadorf",
            postal_code="8355",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Village.objects.create(
                dataset_version=self.dataset_version,
                source_identifier="village-1",
                name="Duplicate Aadorf",
                postal_code="8355",
                canton=self.canton,
                geom=make_test_geometry(),
            )

    def test_blank_village_source_identifier_can_repeat(self) -> None:
        """Blank village source identifiers are allowed for manual records."""
        Village.objects.create(
            dataset_version=self.dataset_version,
            name="Aadorf",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        village = Village.objects.create(
            dataset_version=self.dataset_version,
            name="Ettenhausen",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        self.assertEqual(village.name, "Ettenhausen")

    def test_village_requires_canton_from_same_dataset(self) -> None:
        """Village validation rejects cantons from another dataset version."""
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
        village = Village(
            dataset_version=self.dataset_version,
            name="Invalid Village",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        with self.assertRaises(ValidationError):
            village.full_clean()

    def test_village_requires_municipality_from_same_dataset_and_canton(self) -> None:
        """Village validation rejects incompatible municipality assignments."""
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
            geom=make_test_geometry(),
        )
        village = Village(
            dataset_version=self.dataset_version,
            name="Invalid Village",
            canton=self.canton,
            municipality=other_municipality,
            geom=make_test_geometry(),
        )

        with self.assertRaises(ValidationError):
            village.full_clean()


class GeoSerializerTests(TestCase):
    """Tests for GeoJSON serializer helpers."""

    def setUp(self) -> None:
        """Create shared serializer fixtures."""
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
        )

    def test_feature_collection_skips_objects_without_geometry(self) -> None:
        """Feature collection serialization omits objects with no geometry."""
        data = json.loads(
            feature_collection(
                [self.canton],
                lambda _canton: None,
                lambda canton: {"id": canton.id},
            )
        )

        self.assertEqual(data, {"type": "FeatureCollection", "features": []})

    def test_feature_collection_rejects_non_json_numbers(self) -> None:
        """Feature properties reject invalid JSON values such as NaN."""
        with self.assertRaises(ValueError):
            feature_collection(
                [self.canton],
                lambda canton: canton.geom,
                lambda canton: {"value": float("nan")},
            )

    def test_get_display_geometry_prefers_simplified_geometry(self) -> None:
        """Display geometry uses simplified geometry when it exists."""
        self.assertEqual(get_display_geometry(self.canton), self.canton.geom_simplified)
        self.canton.geom_simplified = None

        self.assertEqual(get_display_geometry(self.canton), self.canton.geom)


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

    def grant_label_access(
        self,
        *,
        revealed: bool = True,
        guest_key: str = "",
    ) -> Turn:
        """Grant the test client session access to municipality labels.

        Args:
            revealed: Whether the linked turn has already been revealed.
            guest_key: Optional guest key that owns the turn.

        Returns:
            The turn tied to the label access grant.
        """
        owner_fields = (
            {"user": None, "guest_key": guest_key}
            if guest_key
            else {"user": self.user, "guest_key": ""}
        )
        game = Game.objects.create(**owner_fields)
        turn = Turn.objects.create(
            game=game,
            turn_number=1,
            municipality_target=self.municipality,
            revealed_at=timezone.now() if revealed else None,
        )
        session = self.client.session
        session[MUNICIPALITY_LABEL_ACCESS_SESSION_KEY] = turn.id
        if guest_key:
            session[GUEST_PLAYER_SESSION_KEY] = guest_key
        session.save()
        return turn

    def municipality_labels_url(self, turn: Turn) -> str:
        """Build the municipality labels URL for a revealed turn.

        Args:
            turn: Turn whose reveal authorizes label access.

        Returns:
            Label endpoint URL with turn context.
        """
        return f"{reverse('geo:municipality_labels_geojson')}?turn={turn.id}"

    def test_boundary_geojson_endpoints_are_public(self) -> None:
        """Anonymous users can load non-sensitive boundary GeoJSON."""
        self.client.logout()
        urls = [
            reverse("geo:cantons_geojson"),
            reverse("geo:municipality_boundaries_geojson"),
        ]

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assert_geojson_response(response)

    def test_municipality_labels_require_owner_session(self) -> None:
        """Anonymous users cannot access labels without an owning session."""
        self.client.logout()

        response = self.client.get(reverse("geo:municipality_labels_geojson"))

        self.assertEqual(response.status_code, 404)

    def test_canton_boundaries_returns_feature_collection(self) -> None:
        """Canton boundary endpoint returns canton properties and geometry."""
        response = self.client.get(reverse("geo:cantons_geojson"))
        data = self.assert_geojson_response(response)

        self.assertEqual(data["type"], "FeatureCollection")
        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["name"], "Zurich")
        self.assertEqual(data["features"][0]["geometry"]["type"], "MultiPolygon")

    def test_canton_boundaries_support_canton_filter(self) -> None:
        """Canton boundary endpoint can return one selected canton."""
        Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=2,
            abbreviation="BE",
            name="Bern",
            geom=make_test_geometry(),
        )

        response = self.client.get(reverse("geo:cantons_geojson"), {"canton": "ZH"})
        data = self.assert_geojson_response(response)

        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["abbreviation"], "ZH")

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

    def test_municipality_boundaries_support_canton_filter(self) -> None:
        """Municipality boundary endpoint can return one selected canton."""
        other_canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=2,
            abbreviation="BE",
            name="Bern",
            geom=make_test_geometry(),
        )
        other_municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=351,
            name="Bern",
            canton=other_canton,
            geom=make_test_geometry(),
        )

        response = self.client.get(
            reverse("geo:municipality_boundaries_geojson"),
            {"canton": "BE"},
        )
        data = self.assert_geojson_response(response)

        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["id"], other_municipality.id)

    def test_municipality_labels_include_reveal_properties(self) -> None:
        """Municipality label endpoint returns names for reveal mode."""
        turn = self.grant_label_access()

        response = self.client.get(self.municipality_labels_url(turn))
        data = self.assert_geojson_response(response)

        self.assertEqual(data["type"], "FeatureCollection")
        self.assertEqual(len(data["features"]), 1)
        feature = data["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Point")
        self.assertEqual(feature["properties"]["id"], self.municipality.id)
        self.assertEqual(feature["properties"]["name"], "Zurich")
        self.assertNotIn("canton_abbreviation", feature["properties"])

    def test_municipality_labels_use_game_canton_scope(self) -> None:
        """Single-canton games reveal labels only for the game's canton."""
        other_canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=2,
            abbreviation="BE",
            name="Bern",
            geom=make_test_geometry(),
        )
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=351,
            name="Bern",
            canton=other_canton,
            geom=make_test_geometry(),
            label_point=Point(7.45, 46.95, srid=4326),
        )
        turn = self.grant_label_access()
        turn.game.mode = Game.Mode.CANTON
        turn.game.canton = self.canton
        turn.game.save(update_fields=["mode", "canton"])

        response = self.client.get(self.municipality_labels_url(turn))
        data = self.assert_geojson_response(response)

        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["name"], "Zurich")

    def test_municipality_labels_allow_guest_reveal_access(self) -> None:
        """Guest players can load labels for their own revealed turn."""
        self.client.logout()
        turn = self.grant_label_access(guest_key="guest-label-key")

        response = self.client.get(self.municipality_labels_url(turn))
        data = self.assert_geojson_response(response)

        self.assertEqual(len(data["features"]), 1)
        self.assertEqual(data["features"][0]["properties"]["name"], "Zurich")

    def test_municipality_labels_skip_missing_label_points(self) -> None:
        """Municipality label endpoint omits municipalities without label points."""
        turn = self.grant_label_access()
        self.municipality.label_point = None
        self.municipality.save(update_fields=["label_point"])

        response = self.client.get(self.municipality_labels_url(turn))
        data = self.assert_geojson_response(response)

        self.assertEqual(data["features"], [])

    def test_municipality_labels_are_not_browser_cacheable(self) -> None:
        """Municipality label responses force browsers to recheck access."""
        turn = self.grant_label_access()

        response = self.client.get(self.municipality_labels_url(turn))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("max-age=3600", response["Cache-Control"])
        self.assertNotIn("ETag", response)

    def test_municipality_labels_require_revealed_turn_access(self) -> None:
        """Municipality labels are unavailable without a current reveal grant."""
        response = self.client.get(reverse("geo:municipality_labels_geojson"))
        self.assertEqual(response.status_code, 404)

        unrevealed_turn = self.grant_label_access(revealed=False)
        response = self.client.get(self.municipality_labels_url(unrevealed_turn))
        self.assertEqual(response.status_code, 404)

    def test_municipality_labels_reject_foreign_turn_access(self) -> None:
        """Users cannot reuse another user's revealed turn to load labels."""
        other_user = get_user_model().objects.create_user(
            username="other-player",
            password="StrongPass123!",
        )
        game = Game.objects.create(user=other_user)
        turn = Turn.objects.create(
            game=game,
            turn_number=1,
            municipality_target=self.municipality,
            revealed_at=timezone.now(),
        )
        session = self.client.session
        session[MUNICIPALITY_LABEL_ACCESS_SESSION_KEY] = turn.id
        session.save()

        response = self.client.get(self.municipality_labels_url(turn))

        self.assertEqual(response.status_code, 404)

    def test_boundary_responses_are_publicly_cacheable(self) -> None:
        """Boundary endpoints allow browser and CDN caching."""
        response = self.client.get(reverse("geo:municipality_boundaries_geojson"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("public", response["Cache-Control"])
        self.assertIn("max-age=300", response["Cache-Control"])
        self.assertIn("stale-while-revalidate=3600", response["Cache-Control"])
        self.assertNotIn("private", response["Cache-Control"])
        self.assertNotIn("no-cache", response["Cache-Control"])
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
        self.assertIn("public", cached_response["Cache-Control"])
        self.assertIn("max-age=300", cached_response["Cache-Control"])

    def test_boundary_conditional_get_accepts_etag_lists_and_wildcards(self) -> None:
        """Boundary endpoints handle common If-None-Match header formats."""
        response = self.client.get(reverse("geo:municipality_boundaries_geojson"))
        etag = response["ETag"]

        for header_value in (f'"stale", {etag}', "*"):
            with self.subTest(header_value=header_value):
                cached_response = self.client.get(
                    reverse("geo:municipality_boundaries_geojson"),
                    HTTP_IF_NONE_MATCH=header_value,
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

    def test_command_rejects_when_only_one_layer_name_is_provided(self) -> None:
        """Command requires both layer names when one explicit layer is passed."""
        source = Path("boundaries.gpkg")
        with mock.patch(
            "geo.management.commands.import_boundaries.resolve_data_source",
            return_value=source,
        ):
            with mock.patch("pyogrio.list_layers", return_value=[("cantons",)]):
                with self.assertRaisesMessage(
                    CommandError,
                    "Both --municipality-layer and --canton-layer are required for import.",
                ):
                    call_command(
                        "import_boundaries",
                        str(source),
                        "--dataset-version",
                        "2026-01-01",
                        "--canton-layer",
                        "cantons",
                        stdout=StringIO(),
                    )

    def test_resolve_data_source_rejects_missing_path(self) -> None:
        """Datasource resolution rejects missing source paths."""
        with self.assertRaisesMessage(CommandError, "Datasource does not exist"):
            resolve_data_source("missing-boundaries-source")

    def test_resolve_data_source_rejects_multiple_candidates(self) -> None:
        """Datasource resolution rejects ambiguous folders with multiple vectors."""
        with TemporaryDirectory() as tmp_dir:
            source_dir = Path(tmp_dir)
            (source_dir / "a.gpkg").write_text("", encoding="utf-8")
            (source_dir / "b.geojson").write_text("{}", encoding="utf-8")
            with self.assertRaisesMessage(
                CommandError,
                "Multiple vector datasources found. Pass one explicitly:",
            ):
                resolve_data_source(str(source_dir))

    def test_list_available_layers_supports_non_sequence_entries(self) -> None:
        """Layer listing handles non-sequence entries from pyogrio."""
        output = StringIO()
        with mock.patch("pyogrio.list_layers", return_value=[123]):
            list_available_layers(Path("boundaries.gpkg"), output)

        self.assertIn("- 123", output.getvalue())

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


class ImportVillagesCommandTests(TestCase):
    """Tests for village/locality import command behavior."""

    def setUp(self) -> None:
        """Create shared boundary fixtures for village imports."""
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

    def village_gdf(self):
        """Return fake village rows for command tests."""
        return gpd.GeoDataFrame(
            [
                {
                    "LOCALITYID": "village-1",
                    "NAME": "Aadorf",
                    "STATUS": "REAL",
                    "VALIDITY": "2026-01-01",
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
                    "LOCALITYID": "liechtenstein-1",
                    "NAME": "Vaduz",
                    "STATUS": "REAL",
                    "VALIDITY": "2026-01-01",
                    "geometry": Polygon(
                        (
                            (9.50, 47.10),
                            (9.55, 47.10),
                            (9.55, 47.15),
                            (9.50, 47.15),
                            (9.50, 47.10),
                        )
                    ),
                },
            ],
            crs="EPSG:4326",
        )

    def test_command_imports_villages_for_current_dataset(self) -> None:
        """Command imports villages and skips rows outside Swiss cantons."""
        output = StringIO()
        with TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir) / "villages.shp"
            source.write_text("", encoding="utf-8")

            with mock.patch(
                "geo.management.commands.import_villages.read_village_layer",
                return_value=self.village_gdf(),
            ):
                call_command("import_villages", str(source), stdout=output)

        village = Village.objects.get()
        self.assertEqual(village.name, "Aadorf")
        self.assertEqual(village.canton, self.canton)
        self.assertEqual(village.municipality, self.municipality)
        self.assertEqual(village.valid_from.isoformat(), "2026-01-01")
        self.assertTrue(village.is_active)
        self.assertIsNotNone(village.label_point)
        self.assertIn("Imported 1 villages", output.getvalue())
        self.assertIn("Skipped 1 rows without Swiss canton", output.getvalue())

    def test_import_villages_updates_existing_source_identifier(self) -> None:
        """Village imports update existing rows with the same source identifier."""
        options = {
            "active_status": "REAL",
            "canton_abbreviation_field": "",
            "canton_bfs_field": "",
            "name_field": "NAME",
            "postal_code_field": "",
            "simplify_tolerance": 0.0,
            "skip_municipality_assignment": False,
            "source_identifier_field": "LOCALITYID",
            "status_field": "STATUS",
            "valid_from_field": "VALIDITY",
            "valid_to_field": "",
        }
        Village.objects.create(
            dataset_version=self.dataset_version,
            source_identifier="village-1",
            name="Old Name",
            canton=self.canton,
            geom=make_test_geometry(),
        )

        result = import_villages(self.village_gdf().iloc[:1], self.dataset_version, options)

        village = Village.objects.get(source_identifier="village-1")
        self.assertEqual(len(result.villages), 1)
        self.assertEqual(village.name, "Aadorf")
        self.assertEqual(Village.objects.count(), 1)

    def test_command_rejects_without_boundary_dataset(self) -> None:
        """Village imports require an existing geodata version."""
        Municipality.objects.all().delete()
        Canton.objects.all().delete()
        GeoDatasetVersion.objects.all().delete()

        with self.assertRaisesMessage(
            CommandError,
            "Import canton and municipality boundaries first.",
        ):
            call_command("import_villages", stdout=StringIO())

    def test_download_asset_rejects_untrusted_hosts(self) -> None:
        """Official village downloads reject hosts outside the allowlist."""
        with self.assertRaisesMessage(CommandError, "URL host 'example.test'"):
            download_village_asset(
                "https://example.test/villages.shp.zip",
                Path("asset.zip"),
            )

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        """Village ZIP extraction rejects archive members outside the target."""
        archive = mock.Mock()
        member = mock.Mock()
        member.filename = "../outside.shp"
        member.external_attr = 0
        member.is_dir.return_value = False
        archive.infolist.return_value = [member]

        with self.assertRaisesMessage(CommandError, "Unsafe path found"):
            safe_extract_village_zip(archive, Path("data/raw/import-test"))

        archive.open.assert_not_called()

    def test_resolve_layer_source_prefers_matching_layer_name(self) -> None:
        """Layer source resolution picks the requested vector from a folder."""
        with TemporaryDirectory() as tmp_dir:
            source = Path(tmp_dir)
            (source / "AMTOVZ_LOCALITY.geojson").write_text("{}", encoding="utf-8")
            (source / "AMTOVZ_ZIP.geojson").write_text("{}", encoding="utf-8")

            resolved = resolve_village_layer_source(
                source,
                "AMTOVZ_LOCALITY",
                source,
            )

        self.assertEqual(resolved.name, "AMTOVZ_LOCALITY.geojson")


class ImportSwissBoundaries3DCommandTests(TestCase):
    """Tests for the official swissBOUNDARIES3D import command."""

    def test_command_rejects_unsupported_stac_url_scheme(self) -> None:
        """Command rejects non-HTTPS STAC item URLs."""
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

    def test_stac_item_selection_supports_latest_and_explicit_versions(self) -> None:
        """STAC item selection chooses explicit versions or newest items."""
        old_item = {"id": "swissboundaries3d_2025-01-01", "properties": {}}
        new_item = {
            "id": "ignored",
            "properties": {"datetime": "2026-01-01T00:00:00Z"},
        }
        items = {"features": [old_item, new_item]}

        self.assertEqual(select_stac_item(items), new_item)
        self.assertEqual(select_stac_item(items, "2025-01-01"), old_item)
        self.assertEqual(dataset_version_label(old_item), "2025-01-01")

        with self.assertRaisesMessage(CommandError, "No swissBOUNDARIES3D item found"):
            select_stac_item(items, "2027-01-01")

    def test_geopackage_asset_selection_requires_matching_asset(self) -> None:
        """GeoPackage asset selection finds valid assets and rejects missing ones."""
        asset = {"href": "https://example.test/data.gpkg.zip", "type": ""}
        item = {"id": "item", "assets": {"download": asset}}

        self.assertEqual(select_geopackage_asset(item), asset)

        with self.assertRaisesMessage(CommandError, "No GeoPackage asset found"):
            select_geopackage_asset(
                {"id": "item", "assets": {"txt": {"href": "readme.txt"}}}
            )

    def test_extract_single_geopackage_requires_exactly_one_gpkg(self) -> None:
        """ZIP extraction rejects archives without exactly one GeoPackage."""
        with TemporaryDirectory() as tmp_dir:
            destination = Path(tmp_dir)
            archive_path = destination / "empty.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("readme.txt", "not a geopackage")

            with self.assertRaisesMessage(CommandError, "Expected one GeoPackage"):
                extract_single_geopackage(archive_path, destination)

    def test_load_stac_items_rejects_unsupported_redirect_scheme(self) -> None:
        """STAC requests reject redirects to non-HTTPS URLs."""
        opener = mock.Mock()
        opener.open.side_effect = [
            make_redirect_error(
                "https://data.geo.admin.ch/items.json",
                "file:///tmp/items.json",
            )
        ]

        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesMessage(
                CommandError,
                "URL scheme 'file' is not allowed.",
            ):
                load_stac_items("https://data.geo.admin.ch/items.json")
        self.assertEqual(opener.open.call_count, 1)

    def test_load_stac_items_reports_invalid_json(self) -> None:
        """STAC requests raise CommandError for malformed JSON responses."""
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=StringIO("<html></html>"))
        response.__exit__ = mock.Mock(return_value=None)

        with mock.patch(
            "geo.management.commands.import_swissboundaries3d."
            "open_url_with_validated_redirects",
            return_value=response,
        ):
            with self.assertRaisesMessage(
                CommandError,
                "Could not parse STAC items response as JSON",
            ):
                load_stac_items("https://data.geo.admin.ch/items.json")

    def test_load_stac_items_rejects_untrusted_hosts(self) -> None:
        """STAC requests reject hosts outside the swisstopo allowlist."""
        with self.assertRaisesMessage(CommandError, "URL host 'example.test'"):
            load_stac_items("https://example.test/items.json")

    def test_download_asset_rejects_unsupported_url_scheme(self) -> None:
        """Asset downloads reject non-HTTPS URLs."""
        with self.assertRaisesMessage(
            CommandError,
            "URL scheme 'file' is not allowed.",
        ):
            download_asset("file:///tmp/boundaries.gpkg.zip", Path("asset.zip"))

    def test_download_asset_rejects_unsupported_redirect_scheme(self) -> None:
        """Asset downloads reject redirects to non-HTTPS URLs."""
        opener = mock.Mock()
        opener.open.side_effect = [
            make_redirect_error(
                "https://data.geo.admin.ch/boundaries.gpkg.zip",
                "file:///tmp/boundaries.gpkg.zip",
            )
        ]

        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesMessage(
                CommandError,
                "URL scheme 'file' is not allowed.",
            ):
                download_asset(
                    "https://data.geo.admin.ch/boundaries.gpkg.zip",
                    Path("asset.zip"),
                )
        self.assertEqual(opener.open.call_count, 1)

    def test_download_asset_rejects_untrusted_hosts(self) -> None:
        """Asset downloads reject hosts outside the swisstopo allowlist."""
        with self.assertRaisesMessage(CommandError, "URL host 'example.test'"):
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
        asset_url = "https://data.geo.admin.ch/swissboundaries3d_2026-01.gpkg.zip"
        stac_items = {
            "features": [
                {
                    "id": "swissboundaries3d_2025-01",
                    "properties": {"datetime": "2025-01-01T00:00:00Z"},
                    "assets": {
                        "old.gpkg.zip": {
                            "href": "https://data.geo.admin.ch/old.gpkg.zip",
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

    def test_resolve_csv_path_requires_existing_file(self) -> None:
        """CSV path resolution rejects missing paths and directories."""
        with TemporaryDirectory() as tmp_dir:
            source_dir = Path(tmp_dir)

            with self.assertRaisesMessage(CommandError, "is not a file"):
                resolve_csv_path(str(source_dir))

            with self.assertRaisesMessage(CommandError, "does not exist"):
                resolve_csv_path(str(source_dir / "missing.csv"))

    def test_resolve_dataset_version_requires_available_dataset(self) -> None:
        """Dataset resolution fails clearly when no target dataset exists."""
        Municipality.objects.all().delete()
        Canton.objects.all().delete()
        GeoDatasetVersion.objects.all().delete()

        with self.assertRaisesMessage(CommandError, "No dataset version found"):
            resolve_dataset_version("swissBOUNDARIES3D", None)

        with self.assertRaisesMessage(CommandError, "Dataset version not found"):
            resolve_dataset_version("swissBOUNDARIES3D", "missing")

    def test_parse_whole_number_handles_required_and_empty_values(self) -> None:
        """Whole-number parser distinguishes required and optional empty cells."""
        self.assertIsNone(
            parse_whole_number("", "population", 2, allow_empty=True)
        )

        with self.assertRaisesMessage(CommandError, "Row 2: bfs_number is required"):
            parse_whole_number("", "bfs_number", 2, allow_empty=False)

    def test_format_bfs_numbers_truncates_long_lists(self) -> None:
        """Missing BFS number formatting remains compact for long lists."""
        formatted = format_bfs_numbers(list(range(1, 23)))

        self.assertIn("1, 2, 3", formatted)
        self.assertIn("... (2 more)", formatted)

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

    def create_municipality(
        self,
        bfs_number: int,
        name: str,
        *,
        is_active: bool = True,
    ) -> Municipality:
        """Create one municipality in the shared dataset version.

        Args:
            bfs_number: Municipality BFS number.
            name: Municipality display name.
            is_active: Whether the municipality is current.

        Returns:
            Created municipality.
        """
        return Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=bfs_number,
            name=name,
            canton=self.canton,
            is_active=is_active,
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

    def test_parse_statpop_population_csv_rejects_bad_structure_and_duplicates(
        self,
    ) -> None:
        """STATPOP parser rejects missing columns and duplicate municipalities."""
        with self.assertRaisesMessage(CommandError, "missing required columns"):
            parse_statpop_population_csv('"Year","Age - total"\n"2024","1"\n')

        duplicate_csv = "\n".join(
            [
                (
                    '"Canton (-) / District (>>) / Commune (......)",'
                    '"Age - total"'
                ),
                '"......0261 Zurich","443000"',
                '"......0261 Zurich","443001"',
            ]
        )
        with self.assertRaisesMessage(CommandError, "duplicate BFS number 261"):
            parse_statpop_population_csv(duplicate_csv)

    def test_statpop_value_parsers_handle_labels_and_grouped_numbers(self) -> None:
        """STATPOP value helpers parse municipality labels and grouped numbers."""
        self.assertEqual(parse_municipality_bfs_number("......0261 Zurich"), 261)
        self.assertIsNone(parse_municipality_bfs_number("- Zurich"))
        self.assertEqual(parse_population_value("443'000", 2), 443000)
        self.assertEqual(parse_population_value("443 000", 2), 443000)

        with self.assertRaisesMessage(CommandError, "population value is required"):
            parse_population_value("", 2)

    def test_statpop_metadata_year_selection_and_query_shape(self) -> None:
        """STATPOP metadata helpers select years and build the expected query."""
        metadata = {"variables": [{"code": "Jahr", "values": ["2022", "2024"]}]}

        self.assertEqual(select_statpop_year(metadata, "latest"), "2024")
        self.assertEqual(select_statpop_year(metadata, "2022"), "2022")
        self.assertEqual(metadata_variable(metadata, "Jahr")["values"], ["2022", "2024"])
        self.assertEqual(build_statpop_query("2024")["query"][0]["selection"]["values"], ["2024"])

        with self.assertRaisesMessage(CommandError, "year is not available"):
            select_statpop_year(metadata, "2023")
        with self.assertRaisesMessage(CommandError, "missing variable"):
            metadata_variable(metadata, "missing")

    def test_decode_statpop_response_falls_back_to_cp1252(self) -> None:
        """STATPOP response decoding supports legacy encoded CSV responses."""
        self.assertEqual(decode_statpop_response("Zürich".encode("cp1252")), "Zürich")

    def test_statpop_requests_reject_untrusted_urls_and_redirects(self) -> None:
        """STATPOP HTTP helpers only allow the trusted BFS HTTPS host."""
        with self.assertRaisesMessage(CommandError, "URL scheme 'http' is not allowed"):
            fetch_statpop_metadata("http://www.pxweb.bfs.admin.ch/table.px")
        with self.assertRaisesMessage(CommandError, "URL host 'example.test'"):
            fetch_statpop_csv("https://example.test/table.px", "2024")

        opener = mock.Mock()
        opener.open.side_effect = [
            make_redirect_error(
                "https://www.pxweb.bfs.admin.ch/table.px",
                "https://example.test/table.px",
            )
        ]
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesMessage(CommandError, "URL host 'example.test'"):
                fetch_statpop_metadata("https://www.pxweb.bfs.admin.ch/table.px")
        self.assertEqual(opener.open.call_count, 1)

        opener = mock.Mock()
        opener.open.side_effect = [
            make_redirect_error(
                "https://www.pxweb.bfs.admin.ch/table.px",
                "https://example.test/table.px",
            )
        ]
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesMessage(CommandError, "URL host 'example.test'"):
                fetch_statpop_csv("https://www.pxweb.bfs.admin.ch/table.px", "2024")
        self.assertEqual(opener.open.call_count, 1)

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

    def test_apply_population_aggregation_rejects_partial_source_sets(self) -> None:
        """Known municipality mutations must have complete source data."""
        with self.assertRaisesMessage(CommandError, "Cannot aggregate STATPOP mutation"):
            apply_population_aggregation({1057: 500})

    def test_population_rows_for_dataset_only_reports_active_missing_values(self) -> None:
        """Population row preparation ignores inactive municipalities."""
        self.create_municipality(261, "Zurich")
        self.create_municipality(9999, "Inactive", is_active=False)

        rows, missing_bfs_numbers = population_rows_for_dataset(
            self.dataset_version,
            {261: 443000},
        )

        self.assertEqual(rows, [{"bfs_number": "261", "population": "443000"}])
        self.assertEqual(missing_bfs_numbers, [])

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

    def test_command_ignores_inactive_municipalities_for_missing_values(self) -> None:
        """Command only requires STATPOP values for active municipalities."""
        zurich = self.create_municipality(261, "Zurich")
        inactive = self.create_municipality(
            9999,
            "Inactive Municipality",
            is_active=False,
        )
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
        output = StringIO()

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
            call_command(
                "import_statpop_population",
                "--dataset-version",
                "2026-01-01",
                stdout=output,
            )

        zurich.refresh_from_db()
        inactive.refresh_from_db()
        self.assertEqual(zurich.population, 443000)
        self.assertIsNone(inactive.population)
        self.assertIn("Updated 1 municipalities", output.getvalue())

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

    def test_setup_validation_ignores_inactive_municipality_population(self) -> None:
        """Setup validation only requires population for active municipalities."""
        active_municipality = self.create_imported_dataset(
            municipality_population=443000
        )
        Municipality.objects.create(
            dataset_version=active_municipality.dataset_version,
            bfs_number=9999,
            name="Inactive Municipality",
            canton=active_municipality.canton,
            is_active=False,
            geom=make_test_geometry(),
        )

        municipality_count, missing_population_count = validate_setup_result(
            active_municipality.dataset_version,
            allow_incomplete_population=False,
        )

        self.assertEqual(municipality_count, 1)
        self.assertEqual(missing_population_count, 0)

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

    def test_resolve_setup_dataset_version_rejects_missing_requested_version(self) -> None:
        """Dataset-version resolution fails when requested imported version is absent."""
        with self.assertRaisesMessage(
            CommandError,
            "Dataset version not found after boundary import:",
        ):
            resolve_setup_dataset_version("2029-01-01")

    def test_validate_setup_result_rejects_missing_cantons(self) -> None:
        """Setup validation requires at least one canton for a dataset version."""
        dataset_version = GeoDatasetVersion.objects.create(
            name=OFFICIAL_BOUNDARIES_DATASET_NAME,
            version_label="2026-01-01",
        )

        with self.assertRaisesMessage(CommandError, "has no cantons."):
            validate_setup_result(dataset_version, allow_incomplete_population=False)

    def test_validate_setup_result_rejects_missing_active_municipalities(self) -> None:
        """Setup validation requires at least one active municipality."""
        municipality = self.create_imported_dataset(municipality_population=443000)
        municipality.is_active = False
        municipality.save(update_fields=["is_active"])

        with self.assertRaisesMessage(CommandError, "has no municipalities."):
            validate_setup_result(
                municipality.dataset_version,
                allow_incomplete_population=False,
            )


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
        self.assertEqual(get_current_dataset_version(), dataset_version)
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

    def test_command_refreshes_dev_timestamp_without_overriding_regular_dataset(
        self,
    ) -> None:
        """Seed command updates dev data without overriding regular geodata."""
        call_command("seed_dev_geodata", stdout=StringIO())
        regular_dataset = GeoDatasetVersion.objects.create(
            name="newer-dataset",
            version_label="local",
        )

        call_command("seed_dev_geodata", stdout=StringIO())
        dev_dataset = GeoDatasetVersion.objects.get(
            name=DATASET_NAME,
            version_label=DATASET_VERSION,
        )

        self.assertGreater(dev_dataset.imported_at, regular_dataset.imported_at)
        self.assertEqual(get_current_dataset_version(), regular_dataset)


class GeodataAdminSetupTests(TestCase):
    """Tests for the geodata setup admin page."""

    def setUp(self) -> None:
        """Create an admin user and a small imported geodata status fixture."""
        user_model = get_user_model()
        self.admin_user = user_model.objects.create_superuser(
            username="geo-admin",
            password="test",
        )
        self.client.force_login(self.admin_user)
        self.dataset_version = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
        )
        canton = Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=canton,
            population=443000,
            geom=make_test_geometry(),
            is_active=True,
        )
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=262,
            name="Missing Population",
            canton=canton,
            geom=make_test_geometry(),
            is_active=True,
        )
        Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=263,
            name="Inactive",
            canton=canton,
            geom=make_test_geometry(),
            is_active=False,
        )

    def test_admin_setup_page_shows_current_geodata_status(self) -> None:
        """Admin setup page renders dataset status counts."""
        response = self.client.get(reverse("admin_geodata_setup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Geodata setup")
        self.assertContains(response, "swissBOUNDARIES3D 2026-01-01")
        self.assertContains(response, "Active municipalities")
        self.assertContains(response, "Active municipalities without population")
        self.assertContains(response, "data-geodata-update-form")
        self.assertContains(response, "data-geodata-action")
        self.assertContains(response, "data-geodata-loading")
        self.assertContains(response, "Geodata update is running")
        self.assertContains(response, "Geodata update progress")
        self.assertContains(response, "requestAnimationFrame")
        self.assertContains(response, ">2<", html=False)
        self.assertContains(response, ">1<", html=False)

    def test_admin_index_shows_geodata_status_link(self) -> None:
        """Admin index exposes a compact geodata setup entry point."""
        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Geodata")
        self.assertContains(response, "swissBOUNDARIES3D 2026-01-01")
        self.assertContains(response, "2 active municipalities")
        self.assertContains(response, "1 without population")
        self.assertContains(response, reverse("admin_geodata_setup"))

    def test_dataset_version_changelist_links_to_setup_page(self) -> None:
        """Dataset version admin changelist exposes the setup page link."""
        response = self.client.get(reverse("admin:geo_geodatasetversion_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("admin_geodata_setup"))
        self.assertContains(response, "Geodata setup")

    def test_admin_setup_requires_staff_access(self) -> None:
        """Anonymous users cannot access the admin setup page."""
        self.client.logout()

        response = self.client.get(reverse("admin_geodata_setup"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_admin_setup_rejects_logged_in_non_staff_users(self) -> None:
        """Logged-in non-staff users cannot access the admin setup page."""
        regular_user = get_user_model().objects.create_user(
            username="regular-user",
            password="test",
        )
        self.client.force_login(regular_user)

        response = self.client.get(reverse("admin_geodata_setup"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_admin_setup_can_run_development_seed_action(self) -> None:
        """Development setup action runs only the seed command."""
        with mock.patch("geo.admin_views.call_command") as call_command:
            response = self.client.post(
                reverse("admin_geodata_setup"),
                {"action": "seed_dev_geodata"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        call_command.assert_called_once()
        self.assertEqual(call_command.call_args.args[0], "seed_dev_geodata")
        self.assertContains(response, "Development geodata update completed.")

    def test_admin_setup_can_run_official_setup_action(self) -> None:
        """Official setup action runs the combined geodata setup command."""
        with mock.patch("geo.admin_views.call_command") as call_command:
            response = self.client.post(
                reverse("admin_geodata_setup"),
                {"action": "setup_geodata"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        call_command.assert_called_once()
        self.assertEqual(call_command.call_args.args[0], "setup_geodata")
        self.assertIs(call_command.call_args.kwargs["keep_existing"], True)
        self.assertContains(response, "Official geodata update completed.")

    def test_admin_setup_rejects_unknown_actions(self) -> None:
        """Unknown admin setup actions do not execute management commands."""
        with mock.patch("geo.admin_views.call_command") as call_command:
            response = self.client.post(
                reverse("admin_geodata_setup"),
                {"action": "unknown"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        call_command.assert_not_called()
        self.assertContains(response, "Unknown geodata action.")

    def test_command_output_truncation_respects_limit(self) -> None:
        """Admin command output truncation never exceeds the requested limit."""
        self.assertEqual(truncate_command_output("abcdef", limit=6), "abcdef")
        self.assertEqual(truncate_command_output("abcdef", limit=5), "...ef")
        self.assertEqual(truncate_command_output("abcdef", limit=3), "def")


class GeoSelectorTests(TestCase):
    """Tests for geodata query helpers."""

    def setUp(self) -> None:
        """Create multiple dataset versions for selector coverage."""
        self.older_dataset = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2025-01-01",
        )
        self.current_dataset = GeoDatasetVersion.objects.create(
            name="swissBOUNDARIES3D",
            version_label="2026-01-01",
        )
        self.older_canton = Canton.objects.create(
            dataset_version=self.older_dataset,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        self.current_canton_b = Canton.objects.create(
            dataset_version=self.current_dataset,
            bfs_number=2,
            abbreviation="BE",
            name="Bern",
            geom=make_test_geometry(),
        )
        self.current_canton_z = Canton.objects.create(
            dataset_version=self.current_dataset,
            bfs_number=1,
            abbreviation="ZH",
            name="Zurich",
            geom=make_test_geometry(),
        )
        Municipality.objects.create(
            dataset_version=self.older_dataset,
            bfs_number=261,
            name="Old Zurich",
            canton=self.older_canton,
            geom=make_test_geometry(),
            is_active=True,
            label_point=Point(8.05, 47.05, srid=4326),
        )
        self.current_active_with_label = Municipality.objects.create(
            dataset_version=self.current_dataset,
            bfs_number=351,
            name="Bern",
            canton=self.current_canton_b,
            geom=make_test_geometry(),
            is_active=True,
            label_point=Point(7.45, 46.95, srid=4326),
        )
        self.current_active_without_label = Municipality.objects.create(
            dataset_version=self.current_dataset,
            bfs_number=352,
            name="Ittigen",
            canton=self.current_canton_b,
            geom=make_test_geometry(),
            is_active=True,
        )
        self.current_inactive_with_label = Municipality.objects.create(
            dataset_version=self.current_dataset,
            bfs_number=353,
            name="Inactive",
            canton=self.current_canton_z,
            geom=make_test_geometry(),
            is_active=False,
            label_point=Point(8.55, 47.37, srid=4326),
        )

    def test_get_current_dataset_version_returns_newest_import(self) -> None:
        """Current dataset selector returns the newest imported version."""
        self.assertEqual(get_current_dataset_version(), self.current_dataset)

    def test_get_current_dataset_version_prefers_official_over_newer_dev_seed(
        self,
    ) -> None:
        """Development seed data is only current when no regular dataset exists."""
        dev_dataset = GeoDatasetVersion.objects.create(
            name=DATASET_NAME,
            version_label=DATASET_VERSION,
        )
        GeoDatasetVersion.objects.filter(pk=dev_dataset.pk).update(
            imported_at=timezone.now() + timedelta(minutes=1)
        )
        dev_dataset.refresh_from_db()

        self.assertGreater(dev_dataset.imported_at, self.current_dataset.imported_at)
        self.assertEqual(get_current_dataset_version(), self.current_dataset)

    def test_get_current_cantons_returns_ordered_current_dataset(self) -> None:
        """Current canton selector uses the newest dataset and abbreviation order."""
        self.assertEqual(
            list(get_current_cantons()),
            [self.current_canton_b, self.current_canton_z],
        )
        self.assertEqual(
            list(get_cantons_for_dataset(self.older_dataset)),
            [self.older_canton],
        )

    def test_get_current_municipalities_filters_to_active_current_rows(self) -> None:
        """Current municipality selector excludes inactive and older datasets."""
        self.assertEqual(
            list(get_current_municipalities()),
            [self.current_active_with_label, self.current_active_without_label],
        )
        self.assertEqual(
            list(get_municipalities_for_dataset(self.current_dataset)),
            [self.current_active_with_label, self.current_active_without_label],
        )

    def test_get_municipality_labels_for_dataset_requires_active_labels(self) -> None:
        """Label selector only returns active municipalities with label points."""
        self.assertEqual(
            list(get_municipality_labels_for_dataset(self.current_dataset)),
            [self.current_active_with_label],
        )
