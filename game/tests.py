"""Tests for the game app."""

from datetime import timedelta
import json
import math
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from geo.constants import MUNICIPALITY_LABEL_ACCESS_SESSION_KEY
from geo.models import Canton, GeoDatasetVersion, Municipality
from tests.utils import make_test_geometry
from tracking.models import GameEvent

from .identity import GUEST_PLAYER_SESSION_KEY, PlayerIdentity, get_player_identity
from .models import Game, Guess, Turn
from .scoring import calculate_score
from .services import (
    GuessSubmissionError,
    InvalidGameModeError,
    InvalidGuessCoordinatesError,
    NotEnoughMunicipalitiesError,
    _calculate_guess_distances,
    _ensure_game_scoring_max_distance_m,
    _normalize_coordinate,
    _normalize_turn_id,
    start_game,
    start_game_for_player,
    submit_guess,
    submit_guess_for_player,
)
from .selectors import (
    get_active_game,
    get_active_game_for_player,
    get_current_turn,
    get_finished_games_for_player,
    get_finished_game_summary,
)
from .statistics import build_player_statistics
from .views import build_summary_reveals, get_last_guess_result, parse_tracking_request


class ScoringTests(TestCase):
    """Tests for game scoring helpers."""

    map_max_distance_m = 410_779

    def test_calculate_score_returns_maximum_for_exact_hit(self) -> None:
        """An exact hit receives the maximum score."""
        self.assertEqual(calculate_score(0, self.map_max_distance_m), 1000)

    def test_calculate_score_caps_near_misses_below_maximum(self) -> None:
        """Only guesses inside the municipality receive the maximum score."""
        self.assertEqual(calculate_score(1, self.map_max_distance_m), 999)

    def test_calculate_score_decays_with_distance(self) -> None:
        """Scores decay by the map extent with a strict curve."""
        self.assertEqual(calculate_score(5_000, self.map_max_distance_m), 784)
        self.assertEqual(calculate_score(25_000, self.map_max_distance_m), 296)
        self.assertEqual(calculate_score(100_000, self.map_max_distance_m), 8)

    def test_calculate_score_never_returns_negative_values(self) -> None:
        """Extremely large valid distances are clamped to zero."""
        self.assertEqual(calculate_score(1_000_000_000, self.map_max_distance_m), 0)

    def test_calculate_score_rejects_negative_distance(self) -> None:
        """Negative distances are invalid."""
        with self.assertRaises(ValueError):
            calculate_score(-1, self.map_max_distance_m)

    def test_calculate_score_rejects_non_finite_distance(self) -> None:
        """Infinite and NaN distances are invalid."""
        for distance in (float("inf"), float("nan")):
            with self.subTest(distance=distance):
                with self.assertRaises(ValueError):
                    calculate_score(distance, self.map_max_distance_m)

    def test_calculate_score_rejects_invalid_map_extent(self) -> None:
        """The scoring map extent must be a positive finite distance."""
        for map_max_distance_m in (0, -1, float("inf"), float("nan")):
            with self.subTest(map_max_distance_m=map_max_distance_m):
                with self.assertRaises(ValueError):
                    calculate_score(100, map_max_distance_m)


class GameServiceHelperTests(TestCase):
    """Tests for low-level game service validation helpers."""

    def create_distance_target(
        self,
        coordinates: tuple[tuple[float, float], ...],
    ) -> Municipality:
        """Create a municipality target with exact WGS84 test geometry."""
        dataset_version = GeoDatasetVersion.objects.create(
            name="distance-test",
            version_label="test",
        )
        geometry = MultiPolygon(Polygon(coordinates, srid=4326), srid=4326)
        canton = Canton.objects.create(
            dataset_version=dataset_version,
            bfs_number=1,
            abbreviation="DT",
            name="Distance Test",
            geom=geometry,
        )
        return Municipality.objects.create(
            dataset_version=dataset_version,
            bfs_number=1,
            name="Distance Target",
            canton=canton,
            geom=geometry,
        )

    def test_normalize_coordinate_accepts_bounds(self) -> None:
        """Coordinate normalization accepts inclusive boundary values."""
        self.assertEqual(
            _normalize_coordinate("-90", name="Latitude", minimum=-90, maximum=90),
            -90,
        )
        self.assertEqual(
            _normalize_coordinate("180", name="Longitude", minimum=-180, maximum=180),
            180,
        )

    def test_normalize_coordinate_rejects_out_of_range_values(self) -> None:
        """Coordinate normalization rejects values outside allowed bounds."""
        with self.assertRaisesMessage(
            InvalidGuessCoordinatesError,
            "Latitude must be between -90 and 90.",
        ):
            _normalize_coordinate("90.1", name="Latitude", minimum=-90, maximum=90)

    def test_normalize_turn_id_accepts_positive_integer_strings(self) -> None:
        """Turn identifier normalization accepts positive integer strings."""
        self.assertEqual(_normalize_turn_id("12"), 12)

    def test_normalize_turn_id_rejects_zero_and_non_integer_values(self) -> None:
        """Turn identifier normalization rejects invalid primary keys."""
        for value in ("0", "not-a-number", None):
            with self.subTest(value=value):
                with self.assertRaises(GuessSubmissionError):
                    _normalize_turn_id(value)

    def test_calculate_guess_distances_rejects_missing_targets(self) -> None:
        """Distance calculation fails clearly for missing municipality targets."""
        with self.assertRaisesMessage(
            GuessSubmissionError,
            "Target municipality does not exist.",
        ):
            _calculate_guess_distances(point=Point(8.0, 47.0, srid=4326), target_id=0)

    def test_calculate_guess_distances_matches_known_meridian_distance(self) -> None:
        """Distance calculation matches a known WGS84 meridian distance."""
        target = self.create_distance_target(
            ((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))
        )

        distances = _calculate_guess_distances(
            point=Point(0, 2, srid=4326),
            target_id=target.id,
        )

        self.assertAlmostEqual(
            distances.distance_to_municipality_m,
            110_575.06354905,
            delta=0.01,
        )
        self.assertAlmostEqual(
            distances.distance_to_boundary_m,
            110_575.06354905,
            delta=0.01,
        )
        self.assertAlmostEqual(
            distances.nearest_boundary_point.x,
            0.0001523435,
            delta=0.000001,
        )
        self.assertAlmostEqual(
            distances.nearest_boundary_point.y,
            1.0000000232,
            delta=0.000001,
        )

    def test_calculate_guess_distances_matches_known_swiss_latitude_distance(
        self,
    ) -> None:
        """Distance calculation matches a known WGS84 east-west distance."""
        target = self.create_distance_target(
            (
                (8.0, 47.0),
                (8.1, 47.0),
                (8.1, 47.1),
                (8.0, 47.1),
                (8.0, 47.0),
            )
        )

        distances = _calculate_guess_distances(
            point=Point(8.2, 47.05, srid=4326),
            target_id=target.id,
        )

        self.assertAlmostEqual(
            distances.distance_to_municipality_m,
            7_598.50117843,
            delta=0.01,
        )
        self.assertAlmostEqual(
            distances.distance_to_boundary_m,
            7_598.50117843,
            delta=0.01,
        )
        self.assertAlmostEqual(
            distances.nearest_boundary_point.x,
            8.1,
            delta=0.000001,
        )
        self.assertAlmostEqual(
            distances.nearest_boundary_point.y,
            47.0500435216,
            delta=0.000001,
        )

    def test_calculate_guess_distances_returns_zero_inside_polygon(self) -> None:
        """Guess points inside the municipality have zero target distance."""
        target = self.create_distance_target(
            (
                (8.0, 47.0),
                (8.1, 47.0),
                (8.1, 47.1),
                (8.0, 47.1),
                (8.0, 47.0),
            )
        )

        distances = _calculate_guess_distances(
            point=Point(8.05, 47.05, srid=4326),
            target_id=target.id,
        )

        self.assertAlmostEqual(distances.distance_to_municipality_m, 0, delta=0.01)
        self.assertGreater(distances.distance_to_boundary_m, 0)
        boundary_point = distances.nearest_boundary_point
        on_vertical_edge = any(
            abs(boundary_point.x - edge) < 0.000001 for edge in (8.0, 8.1)
        )
        on_horizontal_edge = any(
            abs(boundary_point.y - edge) < 0.000001 for edge in (47.0, 47.1)
        )
        self.assertTrue(
            on_vertical_edge or on_horizontal_edge,
            "Nearest point must lie on the municipality boundary.",
        )


class PlayerIdentityTests(TestCase):
    """Tests for authenticated and guest player identity helpers."""

    def setUp(self) -> None:
        """Create shared identity fixtures."""
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(
            username="player",
            password="test",
        )

    def add_session(self, request, *, save: bool = False) -> None:
        """Attach a writable session to a request factory request."""
        middleware = SessionMiddleware(lambda _request: None)
        middleware.process_request(request)
        if save:
            request.session.save()

    def test_authenticated_identity_uses_user_owner_fields(self) -> None:
        """Authenticated identities map to user-owned model fields."""
        identity = PlayerIdentity.for_user(self.user)

        self.assertTrue(identity.is_authenticated)
        self.assertFalse(identity.is_guest)
        self.assertEqual(
            identity.model_fields(),
            {"user": self.user, "guest_key": ""},
        )

    def test_guest_identity_uses_guest_owner_fields(self) -> None:
        """Guest identities map to guest-owned model fields."""
        identity = PlayerIdentity.for_guest("guest-session")

        self.assertFalse(identity.is_authenticated)
        self.assertTrue(identity.is_guest)
        self.assertEqual(
            identity.model_fields(),
            {"user": None, "guest_key": "guest-session"},
        )

    def test_request_identity_can_create_anonymous_session(self) -> None:
        """Anonymous request identities can create a browser guest key."""
        request = self.factory.get("/game/")
        request.user = AnonymousUser()
        self.add_session(request)

        identity = get_player_identity(request, create_session=True)

        self.assertTrue(identity.is_guest)
        self.assertTrue(identity.guest_key)
        self.assertEqual(identity.guest_key, request.session[GUEST_PLAYER_SESSION_KEY])

    def test_empty_anonymous_identity_cannot_own_games(self) -> None:
        """Anonymous requests without a guest key are not game owners yet."""
        request = self.factory.get("/game/")
        request.user = AnonymousUser()
        self.add_session(request)

        identity = get_player_identity(request)

        self.assertFalse(identity.can_own_games)


class GameModelTests(TestCase):
    """Tests for game, turn, and guess model behavior."""

    def setUp(self) -> None:
        """Create shared model fixtures."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="player", password="test")
        self.other_user = user_model.objects.create_user(
            username="other",
            password="test",
        )
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
        self.other_municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=230,
            name="Winterthur",
            canton=self.canton,
            geom=make_test_geometry(),
        )

    def test_game_defaults_to_active(self) -> None:
        """New games start active with zero score."""
        game = Game.objects.create(user=self.user)

        self.assertEqual(game.status, Game.Status.ACTIVE)
        self.assertEqual(game.total_score, 0)
        self.assertIn("Game", str(game))

    def test_guest_game_uses_guest_owner(self) -> None:
        """Games can belong to an anonymous browser session."""
        game = Game.objects.create(user=None, guest_key="guest-session")

        game.full_clean()

        self.assertIsNone(game.user_id)
        self.assertEqual(game.guest_key, "guest-session")
        self.assertIn("guest guest-se", str(game))

    def test_game_requires_exactly_one_owner(self) -> None:
        """Games must belong to either a user or a guest, not both."""
        invalid_games = [
            Game(),
            Game(user=self.user, guest_key="guest-session"),
        ]

        for game in invalid_games:
            with self.subTest(user=game.user, guest_key=game.guest_key):
                with self.assertRaises(ValidationError):
                    game.full_clean()

    def test_game_mode_and_canton_must_match(self) -> None:
        """Games store a canton only for single-canton mode."""
        valid_game = Game(
            user=self.user,
            mode=Game.Mode.CANTON,
            canton=self.canton,
        )

        valid_game.full_clean()

        invalid_games = [
            Game(user=self.user, mode=Game.Mode.SWITZERLAND, canton=self.canton),
            Game(user=self.user, mode=Game.Mode.CANTON),
        ]
        for game in invalid_games:
            with self.subTest(mode=game.mode, canton=game.canton):
                with self.assertRaises(ValidationError):
                    game.full_clean()

    def test_finished_game_requires_finished_at(self) -> None:
        """Finished games require a finish timestamp during validation."""
        game = Game(user=self.user, status=Game.Status.FINISHED)

        with self.assertRaises(ValidationError):
            game.full_clean()

    def test_game_rejects_zero_scoring_max_distance(self) -> None:
        """Scoring map extent must be either empty or strictly positive."""
        game = Game(user=self.user, scoring_max_distance_m=0)

        with self.assertRaises(ValidationError):
            game.full_clean()

    def test_game_rejects_non_finite_scoring_max_distance(self) -> None:
        """Scoring map extent must be finite."""
        for distance in (float("inf"), float("nan")):
            with self.subTest(distance=distance):
                game = Game(user=self.user, scoring_max_distance_m=distance)

                with self.assertRaises(ValidationError):
                    game.full_clean()

    def test_database_rejects_non_finite_scoring_max_distance(self) -> None:
        """Database constraints reject non-finite scoring map extents."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            Game.objects.create(
                user=self.user,
                scoring_max_distance_m=float("inf"),
            )

    def test_database_rejects_multiple_active_games_for_same_user(self) -> None:
        """Only one active game can exist per user."""
        Game.objects.create(user=self.user)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Game.objects.create(user=self.user)

    def test_database_rejects_multiple_active_games_for_same_guest(self) -> None:
        """Only one active game can exist per guest."""
        Game.objects.create(user=None, guest_key="guest-session")

        with self.assertRaises(IntegrityError), transaction.atomic():
            Game.objects.create(user=None, guest_key="guest-session")

    def test_database_allows_finished_and_active_game_for_same_user(self) -> None:
        """A user can start a new game after a previous one is finished."""
        Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            finished_at=timezone.now(),
        )

        Game.objects.create(user=self.user)

        self.assertEqual(Game.objects.filter(user=self.user).count(), 2)

    def test_turn_number_is_unique_per_game(self) -> None:
        """A game cannot contain the same turn number twice."""
        game = Game.objects.create(user=self.user)
        Turn.objects.create(game=game, turn_number=1, target=self.municipality)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Turn.objects.create(game=game, turn_number=1, target=self.other_municipality)

    def test_turn_target_is_unique_per_game(self) -> None:
        """A game cannot target the same municipality twice."""
        game = Game.objects.create(user=self.user)
        Turn.objects.create(game=game, turn_number=1, target=self.municipality)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Turn.objects.create(game=game, turn_number=2, target=self.municipality)

    def test_turn_number_must_be_between_one_and_five(self) -> None:
        """Turn validation rejects numbers outside the five-turn game range."""
        game = Game.objects.create(user=self.user)
        turn = Turn(game=game, turn_number=6, target=self.municipality)

        with self.assertRaises(ValidationError):
            turn.full_clean()

    def test_turn_target_must_be_active(self) -> None:
        """Turn validation rejects inactive municipalities as targets."""
        inactive_municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=999,
            name="Inactive",
            canton=self.canton,
            geom=make_test_geometry(),
            is_active=False,
        )
        game = Game.objects.create(user=self.user)
        turn = Turn(game=game, turn_number=1, target=inactive_municipality)

        with self.assertRaises(ValidationError):
            turn.full_clean()

    def test_guess_is_one_to_one_per_turn(self) -> None:
        """A turn can only have one guess."""
        game = Game.objects.create(user=self.user)
        turn = Turn.objects.create(game=game, turn_number=1, target=self.municipality)
        Guess.objects.create(
            turn=turn,
            user=self.user,
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            Guess.objects.create(
                turn=turn,
                user=self.user,
                point=Point(8.06, 47.06, srid=4326),
                distance_to_municipality_m=100,
                score=900,
            )

    def test_guess_user_must_match_game_user(self) -> None:
        """Guess validation rejects users that do not own the game."""
        game = Game.objects.create(user=self.user)
        turn = Turn.objects.create(game=game, turn_number=1, target=self.municipality)
        guess = Guess(
            turn=turn,
            user=self.other_user,
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        with self.assertRaises(ValidationError):
            guess.full_clean()

    def test_guest_guess_must_match_game_guest(self) -> None:
        """Guest guess validation requires the same guest as the game."""
        game = Game.objects.create(user=None, guest_key="guest-session")
        turn = Turn.objects.create(game=game, turn_number=1, target=self.municipality)
        guess = Guess(
            turn=turn,
            user=None,
            guest_key="other-session",
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        with self.assertRaises(ValidationError):
            guess.full_clean()

    def test_guest_guess_accepts_matching_game_guest(self) -> None:
        """Guest guesses can belong to the same guest as the game."""
        game = Game.objects.create(user=None, guest_key="guest-session")
        turn = Turn.objects.create(game=game, turn_number=1, target=self.municipality)
        guess = Guess(
            turn=turn,
            user=None,
            guest_key="guest-session",
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        guess.full_clean()

    def test_guess_save_derives_user_owner_from_turn_game(self) -> None:
        """Direct guess saves sync user ownership from the linked game."""
        game = Game.objects.create(user=self.user)
        turn = Turn.objects.create(game=game, turn_number=1, target=self.municipality)

        guess = Guess.objects.create(
            turn=turn,
            user=self.other_user,
            guest_key="wrong-guest",
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        guess.refresh_from_db()
        self.assertEqual(guess.user, self.user)
        self.assertEqual(guess.guest_key, "")

    def test_guess_save_derives_guest_owner_from_turn_game(self) -> None:
        """Direct guess saves sync guest ownership from the linked game."""
        game = Game.objects.create(user=None, guest_key="guest-session")
        turn = Turn.objects.create(game=game, turn_number=1, target=self.municipality)

        guess = Guess.objects.create(
            turn=turn,
            user=self.user,
            guest_key="wrong-guest",
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        guess.refresh_from_db()
        self.assertIsNone(guess.user_id)
        self.assertEqual(guess.guest_key, "guest-session")


class GuessSubmissionServiceTests(TestCase):
    """Tests for server-side guess submission behavior."""

    def setUp(self) -> None:
        """Create shared guess submission fixtures."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="player", password="test")
        self.other_user = user_model.objects.create_user(
            username="other",
            password="test",
        )
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

    def create_game_with_turns(
        self,
        turn_count: int = 1,
        status: str = Game.Status.ACTIVE,
    ) -> tuple[Game, list[Turn]]:
        """Create a game with unique target municipalities.

        Args:
            turn_count: Number of turns to create.
            status: Initial game status.

        Returns:
            A tuple of the created game and ordered turns.
        """
        finished_at = timezone.now() if status == Game.Status.FINISHED else None
        game = Game.objects.create(
            user=self.user,
            status=status,
            finished_at=finished_at,
        )
        turns = []
        for index in range(turn_count):
            municipality = Municipality.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=260 + index,
                name=f"Municipality {index + 1}",
                canton=self.canton,
                geom=make_test_geometry(),
            )
            turns.append(
                Turn.objects.create(
                    game=game,
                    turn_number=index + 1,
                    target=municipality,
                )
            )
        return game, turns

    def test_submit_guess_persists_exact_hit_and_starts_next_turn(self) -> None:
        """Submitting an exact hit creates a guess and starts the next turn."""
        game, turns = self.create_game_with_turns(turn_count=2)

        result = submit_guess(self.user, turns[0].id, 47.05, 8.05)

        result.guess.refresh_from_db()
        game.refresh_from_db()
        turns[0].refresh_from_db()
        self.assertEqual(result.game.pk, game.pk)
        self.assertEqual(result.turn.pk, turns[0].pk)
        self.assertEqual(result.guess.user, self.user)
        self.assertEqual(result.guess.point.x, 8.05)
        self.assertEqual(result.guess.point.y, 47.05)
        self.assertAlmostEqual(result.guess.distance_to_municipality_m, 0, places=3)
        self.assertGreater(result.guess.distance_to_boundary_m, 0)
        self.assertIsNotNone(result.guess.nearest_boundary_point)
        self.assertIsNotNone(game.scoring_max_distance_m)
        self.assertEqual(result.guess.score, 1000)
        self.assertIsNotNone(turns[0].revealed_at)
        self.assertEqual(game.total_score, 1000)
        self.assertEqual(game.status, Game.Status.ACTIVE)
        self.assertTrue(
            GameEvent.objects.filter(
                user=self.user,
                game=game,
                turn=turns[0],
                event_type=GameEvent.Type.GUESS_CONFIRMED,
                payload__score=1000,
            ).exists()
        )
        self.assertTrue(
            GameEvent.objects.filter(
                user=self.user,
                game=game,
                turn=turns[1],
                event_type=GameEvent.Type.TURN_STARTED,
                payload={"turn_number": 2},
            ).exists()
        )

    def test_submit_guess_for_player_accepts_guest_owner(self) -> None:
        """Guest identities can submit guesses for guest-owned games."""
        game = Game.objects.create(user=None, guest_key="guest-session")
        municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )
        turn = Turn.objects.create(game=game, turn_number=1, target=municipality)

        result = submit_guess_for_player(
            PlayerIdentity.for_guest("guest-session"),
            turn.id,
            47.05,
            8.05,
        )

        self.assertIsNone(result.guess.user_id)
        self.assertEqual(result.guess.guest_key, "guest-session")
        self.assertTrue(
            GameEvent.objects.filter(
                user__isnull=True,
                guest_key="guest-session",
                game=game,
                event_type=GameEvent.Type.GUESS_CONFIRMED,
            ).exists()
        )

    def test_submit_guess_scores_outside_polygon_distance(self) -> None:
        """Submitting outside the target polygon stores positive distances."""
        game, turns = self.create_game_with_turns()

        result = submit_guess(self.user, turns[0].id, 47.05, 8.2)

        result.guess.refresh_from_db()
        game.refresh_from_db()
        self.assertGreater(result.guess.distance_to_municipality_m, 0)
        self.assertGreater(result.guess.distance_to_boundary_m, 0)
        self.assertIsNotNone(result.guess.nearest_boundary_point)
        self.assertIsNotNone(game.scoring_max_distance_m)
        self.assertLess(result.guess.score, 1000)
        self.assertGreaterEqual(result.guess.score, 0)
        self.assertEqual(game.total_score, result.guess.score)

    def test_submit_guess_persists_legacy_game_scoring_distance(self) -> None:
        """Legacy games missing scoring extent calculate and persist it on guess."""
        game, turns = self.create_game_with_turns()
        game.scoring_max_distance_m = None
        game.save(update_fields=["scoring_max_distance_m"])

        result = submit_guess(self.user, turns[0].id, 47.05, 8.2)

        game.refresh_from_db()
        self.assertIsNotNone(game.scoring_max_distance_m)
        self.assertGreater(game.scoring_max_distance_m, 0)
        self.assertEqual(
            result.guess.score,
            calculate_score(
                result.guess.distance_to_municipality_m,
                game.scoring_max_distance_m,
            ),
        )

    def test_ensure_game_scoring_distance_repairs_non_finite_value(self) -> None:
        """Non-finite legacy scoring extents are recalculated and persisted."""
        game, turns = self.create_game_with_turns()
        game.scoring_max_distance_m = float("inf")

        scoring_max_distance_m = _ensure_game_scoring_max_distance_m(
            game=game,
            target_id=turns[0].target_id,
        )

        self.assertTrue(math.isfinite(scoring_max_distance_m))
        self.assertGreater(scoring_max_distance_m, 0)
        game.refresh_from_db()
        self.assertEqual(game.scoring_max_distance_m, scoring_max_distance_m)

    def test_submit_guess_finishes_game_after_final_turn(self) -> None:
        """Submitting the final turn marks the game as finished."""
        game, turns = self.create_game_with_turns()

        submit_guess(self.user, turns[0].id, 47.05, 8.05)

        game.refresh_from_db()
        self.assertEqual(game.status, Game.Status.FINISHED)
        self.assertIsNotNone(game.finished_at)
        self.assertTrue(
            GameEvent.objects.filter(
                user=self.user,
                game=game,
                event_type=GameEvent.Type.GAME_FINISHED,
                payload={"total_score": 1000},
            ).exists()
        )

    def test_submit_guess_rejects_invalid_coordinates(self) -> None:
        """Invalid coordinate values are rejected before persistence."""
        _game, turns = self.create_game_with_turns()

        invalid_coordinates = [
            (91, 8.05),
            (47.05, 181),
            ("invalid", 8.05),
            (float("nan"), 8.05),
        ]
        for latitude, longitude in invalid_coordinates:
            with self.subTest(latitude=latitude, longitude=longitude):
                with self.assertRaises(InvalidGuessCoordinatesError):
                    submit_guess(self.user, turns[0].id, latitude, longitude)

        self.assertFalse(Guess.objects.exists())

    def test_submit_guess_rejects_invalid_turn_id(self) -> None:
        """Invalid turn identifiers are rejected before persistence."""
        self.create_game_with_turns()

        invalid_turn_ids = [None, "", "abc", "0"]
        for turn_id in invalid_turn_ids:
            with self.subTest(turn_id=turn_id):
                with self.assertRaises(GuessSubmissionError):
                    submit_guess(self.user, turn_id, 47.05, 8.05)

        self.assertFalse(Guess.objects.exists())

    def test_submit_guess_rejects_wrong_user(self) -> None:
        """Users cannot guess turns owned by another user."""
        _game, turns = self.create_game_with_turns()

        with self.assertRaises(GuessSubmissionError):
            submit_guess(self.other_user, turns[0].id, 47.05, 8.05)

        self.assertFalse(Guess.objects.exists())

    def test_submit_guess_rejects_non_current_turn(self) -> None:
        """Only the first unrevealed turn can be guessed."""
        _game, turns = self.create_game_with_turns(turn_count=2)

        with self.assertRaises(GuessSubmissionError):
            submit_guess(self.user, turns[1].id, 47.05, 8.05)

        self.assertFalse(Guess.objects.exists())

    def test_submit_guess_rejects_already_guessed_turn(self) -> None:
        """A revealed turn cannot receive another guess."""
        _game, turns = self.create_game_with_turns(turn_count=2)
        submit_guess(self.user, turns[0].id, 47.05, 8.05)

        with self.assertRaises(GuessSubmissionError):
            submit_guess(self.user, turns[0].id, 47.05, 8.05)

        self.assertEqual(Guess.objects.count(), 1)

    def test_submit_guess_rejects_finished_game(self) -> None:
        """Finished games cannot receive new guesses."""
        _game, turns = self.create_game_with_turns(status=Game.Status.FINISHED)

        with self.assertRaises(GuessSubmissionError):
            submit_guess(self.user, turns[0].id, 47.05, 8.05)

        self.assertFalse(Guess.objects.exists())


class GameStartTests(TestCase):
    """Tests for game start services and views."""

    def setUp(self) -> None:
        """Create shared game start fixtures."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="player",
            password="StrongPass123!",
        )
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

    def create_municipalities(
        self,
        count: int,
        is_active: bool = True,
        canton: Canton | None = None,
    ) -> list[Municipality]:
        """Create municipalities for game target selection.

        Args:
            count: Number of municipalities to create.
            is_active: Whether created municipalities are active.
            canton: Optional canton for created municipalities.

        Returns:
            Created municipality objects.
        """
        municipalities = []
        canton = canton or self.canton
        existing_count = Municipality.objects.filter(
            dataset_version=self.dataset_version
        ).count()
        for index in range(count):
            municipalities.append(
                Municipality.objects.create(
                    dataset_version=self.dataset_version,
                    bfs_number=1000 + existing_count + index,
                    name=f"Municipality {existing_count + index + 1}",
                    canton=canton,
                    geom=make_test_geometry(),
                    is_active=is_active,
                )
            )
        return municipalities

    def create_canton(self, abbreviation: str, name: str) -> Canton:
        """Create another canton in the current dataset."""
        return Canton.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=100 + Canton.objects.count(),
            abbreviation=abbreviation,
            name=name,
            geom=make_test_geometry(),
        )

    def post_tracking_event(
        self,
        turn: Turn,
        *,
        client=None,
        event_type: str = GameEvent.Type.MAP_CLICKED,
        payload: object | None = None,
    ):
        """Post a JSON tracking event to the turn event endpoint.

        Args:
            turn: Turn receiving the tracking event.
            client: Optional test client.
            event_type: Event type value.
            payload: Optional event payload.

        Returns:
            The endpoint response.
        """
        event_client = client or self.client
        return event_client.post(
            reverse("game:track_turn_event", args=[turn.id]),
            data=json.dumps(
                {
                    "event_type": event_type,
                    "payload": payload if payload is not None else {},
                }
            ),
            content_type="application/json",
        )

    def test_start_game_creates_five_unique_turns_and_events(self) -> None:
        """Starting a game creates five unique turns and start events."""
        self.create_municipalities(6)

        game = start_game(self.user)

        turns = list(game.turns.order_by("turn_number"))
        self.assertEqual(game.status, Game.Status.ACTIVE)
        self.assertIsNotNone(game.scoring_max_distance_m)
        self.assertGreater(game.scoring_max_distance_m, 0)
        self.assertEqual(len(turns), 5)
        self.assertEqual([turn.turn_number for turn in turns], [1, 2, 3, 4, 5])
        self.assertEqual(len({turn.target_id for turn in turns}), 5)
        self.assertEqual(game.mode, Game.Mode.SWITZERLAND)
        self.assertIsNone(game.canton_id)
        self.assertTrue(
            GameEvent.objects.filter(
                user=self.user,
                game=game,
                event_type=GameEvent.Type.GAME_STARTED,
            ).exists()
        )
        self.assertTrue(
            GameEvent.objects.filter(
                user=self.user,
                game=game,
                turn=turns[0],
                event_type=GameEvent.Type.TURN_STARTED,
                payload={"turn_number": 1},
            ).exists()
        )

    def test_start_game_for_player_accepts_guest_owner(self) -> None:
        """Guest identities can own active games at the service layer."""
        self.create_municipalities(5)

        game = start_game_for_player(PlayerIdentity.for_guest("guest-session"))

        self.assertIsNone(game.user_id)
        self.assertEqual(game.guest_key, "guest-session")
        self.assertEqual(game.turns.count(), 5)
        self.assertTrue(
            GameEvent.objects.filter(
                user__isnull=True,
                guest_key="guest-session",
                game=game,
                event_type=GameEvent.Type.GAME_STARTED,
            ).exists()
        )

    def test_start_game_reuses_existing_active_game(self) -> None:
        """Starting again returns the existing active game."""
        self.create_municipalities(5)

        first_game = start_game(self.user)
        second_game = start_game(self.user)

        self.assertEqual(second_game, first_game)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)
        self.assertEqual(GameEvent.objects.filter(game=first_game).count(), 2)

    def test_start_game_for_player_uses_single_canton_scope(self) -> None:
        """Single-canton games only target municipalities from that canton."""
        bern = self.create_canton("BE", "Bern")
        self.create_municipalities(5)
        bern_municipalities = self.create_municipalities(5, canton=bern)

        game = start_game_for_player(
            PlayerIdentity.for_user(self.user),
            mode=Game.Mode.CANTON,
            canton_abbreviation="BE",
        )

        target_canton_ids = set(
            game.turns.values_list("target__canton_id", flat=True)
        )
        self.assertEqual(game.mode, Game.Mode.CANTON)
        self.assertEqual(game.canton, bern)
        self.assertEqual(target_canton_ids, {bern.id})
        self.assertTrue(
            set(game.turns.values_list("target_id", flat=True)).issubset(
                {municipality.id for municipality in bern_municipalities}
            )
        )
        self.assertGreater(game.scoring_max_distance_m, 0)

    def test_start_game_canton_scope_requires_enough_canton_municipalities(self) -> None:
        """Single-canton games count only municipalities from the selected canton."""
        bern = self.create_canton("BE", "Bern")
        self.create_municipalities(4, canton=bern)
        self.create_municipalities(5)

        with self.assertRaises(NotEnoughMunicipalitiesError):
            start_game_for_player(
                PlayerIdentity.for_user(self.user),
                mode=Game.Mode.CANTON,
                canton_abbreviation="BE",
            )

        self.assertFalse(Game.objects.exists())

    def test_start_game_rejects_invalid_mode(self) -> None:
        """Unknown game modes are rejected before game creation."""
        self.create_municipalities(5)

        with self.assertRaises(InvalidGameModeError):
            start_game_for_player(
                PlayerIdentity.for_user(self.user),
                mode="invalid",
            )

        self.assertFalse(Game.objects.exists())

    def test_start_game_requires_five_active_municipalities(self) -> None:
        """Starting a game requires five active municipalities."""
        self.create_municipalities(4)
        self.create_municipalities(1, is_active=False)

        with self.assertRaises(NotEnoughMunicipalitiesError):
            start_game(self.user)

        self.assertEqual(Game.objects.count(), 0)
        self.assertEqual(Turn.objects.count(), 0)

    def test_start_game_rechecks_active_game_before_setup_error(self) -> None:
        """Starting a game resumes a concurrently-created game before erroring."""
        existing_game = Game.objects.create(user=self.user)

        with patch(
            "game.services.get_active_game_for_player",
            side_effect=[None, existing_game],
        ):
            game = start_game(self.user)

        self.assertEqual(game, existing_game)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)

    def test_start_game_recovers_from_active_game_integrity_error(self) -> None:
        """Starting a game resumes the active game after a unique conflict."""
        self.create_municipalities(5)
        existing_game = Game.objects.create(user=self.user)

        with (
            patch(
                "game.services.get_active_game_for_player",
                side_effect=[None, existing_game],
            ),
            patch.object(Game.objects, "select_for_update") as select_for_update,
            patch.object(Game.objects, "create", side_effect=IntegrityError),
        ):
            active_game_query = (
                select_for_update.return_value.filter.return_value.order_by.return_value
            )
            active_game_query.first.return_value = None

            game = start_game(self.user)

        self.assertEqual(game, existing_game)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)

    def test_game_index_shows_start_form_without_active_game(self) -> None:
        """Logged-in game index renders the map shell and start form."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/index.html")
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, "wmts.geo.admin.ch")
        self.assertContains(response, "Start game")
        self.assertContains(response, "Start")
        self.assertContains(response, "Map setup")
        self.assertContains(response, "Switzerland")
        self.assertContains(response, "Single canton")
        self.assertContains(response, "Zurich")
        self.assertContains(response, "data-game-mode-picker")
        self.assertContains(response, 'id="game-start-form"')
        self.assertContains(response, 'name="game_mode"')
        self.assertContains(response, 'name="canton"')
        self.assertContains(response, 'form="game-start-form"')
        self.assertContains(response, reverse("game:start"))

    def test_game_index_shows_auth_prompt_for_anonymous_users(self) -> None:
        """Anonymous users can view the game shell before login."""
        response = self.client.get(reverse("game:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/index.html")
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, "Start game")
        self.assertContains(response, "Map setup")
        self.assertContains(response, "Single canton")
        self.assertContains(response, reverse("geo:cantons_geojson"))
        self.assertContains(response, reverse("geo:municipality_boundaries_geojson"))
        self.assertContains(response, "Play without account")
        self.assertContains(response, "data-guest-mode-choice")
        self.assertContains(response, "data-guest-start-form")
        self.assertContains(response, 'name="game_mode"')
        self.assertContains(response, 'name="canton"')
        self.assertContains(response, 'form="guest-start-form"')
        self.assertContains(response, "data-auth-modal-open")
        self.assertContains(response, reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:register"))
        self.assertContains(response, reverse("game:start"))

    def test_game_index_resumes_guest_game_without_auth_prompt(self) -> None:
        """Guest players with an active game continue without the entry modal."""
        self.create_municipalities(5)
        self.client.post(reverse("game:start"))

        response = self.client.get(reverse("game:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1/5")
        self.assertContains(response, "Guess")
        self.assertNotContains(response, "data-auth-modal-open")
        self.assertNotContains(response, "Play without account")

    def test_start_view_allows_guest_games(self) -> None:
        """Anonymous users can start a guest-owned game."""
        self.create_municipalities(5)

        response = self.client.post(reverse("game:start"))

        self.assertRedirects(response, reverse("game:index"))
        guest_key = self.client.session[GUEST_PLAYER_SESSION_KEY]
        game = Game.objects.get()
        self.assertIsNone(game.user)
        self.assertEqual(game.guest_key, guest_key)
        self.assertEqual(game.turns.count(), 5)
        self.assertEqual(game.mode, Game.Mode.SWITZERLAND)
        self.assertTrue(
            GameEvent.objects.filter(
                user__isnull=True,
                guest_key=guest_key,
                game=game,
                event_type=GameEvent.Type.GAME_STARTED,
            ).exists()
        )

    def test_start_view_starts_canton_game(self) -> None:
        """Game start view accepts a single-canton scope."""
        self.create_municipalities(5)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("game:start"),
            {"game_mode": "canton", "canton": "ZH"},
        )

        self.assertRedirects(response, reverse("game:index"))
        game = Game.objects.get()
        self.assertEqual(game.mode, Game.Mode.CANTON)
        self.assertEqual(game.canton, self.canton)

        game_response = self.client.get(reverse("game:index"))
        self.assertContains(game_response, "ZH")
        self.assertContains(
            game_response,
            (
                'data-municipality-boundaries-url="'
                f'{reverse("geo:municipality_boundaries_geojson")}?canton=ZH"'
            ),
        )

    def test_start_view_ignores_canton_for_switzerland_mode(self) -> None:
        """Switzerland mode ignores a posted canton from non-JS form controls."""
        self.create_municipalities(5)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("game:start"),
            {"game_mode": "switzerland", "canton": "ZH"},
        )

        self.assertRedirects(response, reverse("game:index"))
        game = Game.objects.get()
        self.assertEqual(game.mode, Game.Mode.SWITZERLAND)
        self.assertIsNone(game.canton)

    def test_start_view_rejects_invalid_canton(self) -> None:
        """Game start view validates canton choices."""
        self.create_municipalities(5)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("game:start"),
            {"game_mode": "canton", "canton": "XX"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Choose a valid canton.", status_code=400)
        self.assertFalse(Game.objects.exists())

    def test_start_view_rejects_get(self) -> None:
        """Game start endpoint only accepts POST."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:start"))

        self.assertEqual(response.status_code, 405)

    def test_start_view_requires_csrf(self) -> None:
        """Game start POSTs require CSRF protection."""
        self.create_municipalities(5)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(reverse("game:start"))

        self.assertEqual(response.status_code, 403)

    def test_guess_view_rejects_anonymous_without_guest_game(self) -> None:
        """Anonymous users need a guest game before submitting guesses."""
        response = self.client.post(reverse("game:guess"))

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Player identity cannot submit guesses.",
            status_code=400,
        )
        self.assertFalse(Guess.objects.exists())

    def test_guess_view_rejects_get(self) -> None:
        """Guess endpoint only accepts POST."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:guess"))

        self.assertEqual(response.status_code, 405)

    def test_guess_view_requires_csrf(self) -> None:
        """Guess POSTs require CSRF protection."""
        self.create_municipalities(5)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = csrf_client.post(
            reverse("game:guess"),
            {
                "turn_id": turn.id,
                "latitude": "47.05",
                "longitude": "8.05",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_tracking_event_rejects_anonymous_without_guest_game(self) -> None:
        """Anonymous tracking requests without a matching guest are hidden."""
        self.create_municipalities(5)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(turn)

        self.assertEqual(response.status_code, 404)

    def test_tracking_event_rejects_get(self) -> None:
        """Tracking event endpoint only accepts POST."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.client.get(reverse("game:track_turn_event", args=[turn.id]))

        self.assertEqual(response.status_code, 405)

    def test_tracking_event_requires_csrf(self) -> None:
        """Tracking event POSTs require CSRF protection."""
        self.create_municipalities(5)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        response = self.post_tracking_event(turn, client=csrf_client)

        self.assertEqual(response.status_code, 403)

    def test_tracking_event_stores_allowed_client_events(self) -> None:
        """Tracking event endpoint stores all allowed UI events."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        active_event_types = [GameEvent.Type.MAP_CLICKED]
        revealed_event_types = [
            GameEvent.Type.REVEAL_SHOWN,
            GameEvent.Type.NEXT_TURN_CLICKED,
        ]
        for index, event_type in enumerate(active_event_types, start=1):
            with self.subTest(event_type=event_type):
                response = self.post_tracking_event(
                    turn,
                    event_type=event_type,
                    payload={
                        "event_index": index,
                        "latitude": 47.05,
                        "longitude": 8.05,
                        "zoom": 8,
                    },
                )

                self.assertEqual(response.status_code, 204)
                self.assertTrue(
                    GameEvent.objects.filter(
                        user=self.user,
                        game=game,
                        turn=turn,
                        event_type=event_type,
                        payload__event_index=index,
                    ).exists()
                )

        submit_guess(self.user, turn.id, 47.05, 8.05)
        turn.refresh_from_db()
        for index, event_type in enumerate(revealed_event_types, start=3):
            with self.subTest(event_type=event_type):
                response = self.post_tracking_event(
                    turn,
                    event_type=event_type,
                    payload={
                        "event_index": index,
                        "latitude": 47.05,
                        "longitude": 8.05,
                        "zoom": 8,
                    },
                )

                self.assertEqual(response.status_code, 204)
                self.assertTrue(
                    GameEvent.objects.filter(
                        user=self.user,
                        game=game,
                        turn=turn,
                        event_type=event_type,
                        payload__event_index=index,
                    ).exists()
                )

    def test_tracking_event_rejects_wrong_turn_state(self) -> None:
        """Tracking endpoint rejects events that do not match turn state."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turns = list(game.turns.order_by("turn_number"))

        future_turn_response = self.post_tracking_event(turns[1])
        unrevealed_reveal_response = self.post_tracking_event(
            turns[0],
            event_type=GameEvent.Type.REVEAL_SHOWN,
        )

        self.assertEqual(future_turn_response.status_code, 400)
        self.assertEqual(unrevealed_reveal_response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                event_type__in=[
                    GameEvent.Type.MAP_CLICKED,
                    GameEvent.Type.REVEAL_SHOWN,
                ],
            ).exists()
        )

    def test_tracking_event_rejects_stale_revealed_turn(self) -> None:
        """Tracking endpoint only accepts post-reveal events for latest reveal."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turns = list(game.turns.order_by("turn_number"))
        submit_guess(self.user, turns[0].id, 47.05, 8.05)
        submit_guess(self.user, turns[1].id, 47.05, 8.05)

        stale_reveal_response = self.post_tracking_event(
            turns[0],
            event_type=GameEvent.Type.REVEAL_SHOWN,
        )
        stale_next_turn_response = self.post_tracking_event(
            turns[0],
            event_type=GameEvent.Type.NEXT_TURN_CLICKED,
        )
        latest_reveal_response = self.post_tracking_event(
            turns[1],
            event_type=GameEvent.Type.REVEAL_SHOWN,
        )
        latest_next_turn_response = self.post_tracking_event(
            turns[1],
            event_type=GameEvent.Type.NEXT_TURN_CLICKED,
        )

        self.assertEqual(stale_reveal_response.status_code, 400)
        self.assertEqual(stale_next_turn_response.status_code, 400)
        self.assertEqual(latest_reveal_response.status_code, 204)
        self.assertEqual(latest_next_turn_response.status_code, 204)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turns[0],
                event_type__in=[
                    GameEvent.Type.REVEAL_SHOWN,
                    GameEvent.Type.NEXT_TURN_CLICKED,
                ],
            ).exists()
        )

    def test_tracking_event_rejects_next_turn_for_finished_game(self) -> None:
        """Tracking endpoint rejects next-turn clicks after the final turn."""
        municipality = self.create_municipalities(1)[0]
        self.client.force_login(self.user)
        game = Game.objects.create(user=self.user)
        turn = Turn.objects.create(
            game=game,
            turn_number=1,
            target=municipality,
        )
        submit_guess(self.user, turn.id, 47.05, 8.05)
        turn.refresh_from_db()

        response = self.post_tracking_event(
            turn,
            event_type=GameEvent.Type.NEXT_TURN_CLICKED,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turn,
                event_type=GameEvent.Type.NEXT_TURN_CLICKED,
            ).exists()
        )

    def test_tracking_event_rejects_non_client_event_type(self) -> None:
        """Tracking endpoint rejects server-only event types."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(
            turn,
            event_type=GameEvent.Type.GAME_STARTED,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            GameEvent.objects.filter(
                game=game,
                event_type=GameEvent.Type.GAME_STARTED,
            ).count(),
            1,
        )

    def test_tracking_event_rejects_unemitted_client_event_type(self) -> None:
        """Tracking endpoint rejects client event types not emitted by the UI."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(
            turn,
            event_type=GameEvent.Type.PIN_MOVED,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turn,
                event_type=GameEvent.Type.PIN_MOVED,
            ).exists()
        )

    def test_tracking_event_rejects_foreign_turn(self) -> None:
        """Users cannot post tracking events to another user's turn."""
        other_user = get_user_model().objects.create_user(
            username="other-player",
            password="test",
        )
        self.create_municipalities(5)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()
        self.client.force_login(other_user)

        response = self.post_tracking_event(turn)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            GameEvent.objects.filter(
                user=other_user,
                turn=turn,
                event_type=GameEvent.Type.MAP_CLICKED,
            ).exists()
        )

    def test_tracking_event_rejects_invalid_payload_shape(self) -> None:
        """Tracking payload must be a JSON object."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(turn, payload=["not", "an", "object"])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turn,
                event_type=GameEvent.Type.MAP_CLICKED,
            ).exists()
        )

    def test_tracking_event_rejects_malformed_json(self) -> None:
        """Tracking endpoint rejects malformed JSON request bodies."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.client.post(
            reverse("game:track_turn_event", args=[turn.id]),
            data="{invalid-json",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turn,
                event_type=GameEvent.Type.MAP_CLICKED,
            ).exists()
        )

    def test_tracking_event_rejects_oversized_json(self) -> None:
        """Tracking endpoint rejects request bodies above the size limit."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(
            turn,
            payload={"value": "x" * 4096},
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turn,
                event_type=GameEvent.Type.MAP_CLICKED,
            ).exists()
        )

    def test_tracking_event_rejects_invalid_content_lengths(self) -> None:
        """Tracking endpoint rejects invalid declared content lengths."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        for content_length in ("not-a-number", "-1"):
            with self.subTest(content_length=content_length):
                response = self.client.post(
                    reverse("game:track_turn_event", args=[turn.id]),
                    data=json.dumps(
                        {
                            "event_type": GameEvent.Type.MAP_CLICKED,
                            "payload": {"latitude": 47.05},
                        }
                    ),
                    content_type="application/json",
                    CONTENT_LENGTH=content_length,
                )

                self.assertEqual(response.status_code, 400)
        self.assertFalse(
            GameEvent.objects.filter(
                game=game,
                turn=turn,
                event_type=GameEvent.Type.MAP_CLICKED,
            ).exists()
        )

    def test_tracking_event_rejects_missing_content_length(self) -> None:
        """Tracking parser rejects missing declared content lengths."""
        payload = json.dumps(
            {
                "event_type": GameEvent.Type.MAP_CLICKED,
                "payload": {"latitude": 47.05},
            }
        )
        request = RequestFactory().post(
            reverse("game:track_turn_event", args=[1]),
            data=payload,
            content_type="application/json",
        )
        request.META.pop("CONTENT_LENGTH", None)

        with self.assertRaises(ValueError):
            parse_tracking_request(request)

    def test_start_view_creates_game_and_redirects(self) -> None:
        """Game start endpoint creates a game and redirects to the index."""
        self.create_municipalities(5)
        self.client.force_login(self.user)

        response = self.client.post(reverse("game:start"))

        self.assertRedirects(response, reverse("game:index"))
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Turn.objects.count(), 5)

    def test_guess_view_submits_current_turn_and_redirects(self) -> None:
        """Guess endpoint submits a valid current turn guess."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.client.post(
            reverse("game:guess"),
            {
                "turn_id": turn.id,
                "latitude": "47.05",
                "longitude": "8.05",
            },
        )

        self.assertRedirects(response, reverse("game:index"))
        turn.refresh_from_db()
        game.refresh_from_db()
        self.assertIsNotNone(turn.revealed_at)
        self.assertEqual(game.total_score, 1000)
        self.assertTrue(Guess.objects.filter(turn=turn, user=self.user).exists())

    def test_guess_view_submits_guest_current_turn(self) -> None:
        """Guest players can submit guesses for their guest-owned game."""
        self.create_municipalities(5)
        self.client.post(reverse("game:start"))
        guest_key = self.client.session[GUEST_PLAYER_SESSION_KEY]
        game = Game.objects.get(guest_key=guest_key)
        turn = game.turns.order_by("turn_number").first()

        response = self.client.post(
            reverse("game:guess"),
            {
                "turn_id": turn.id,
                "latitude": "47.05",
                "longitude": "8.05",
            },
        )

        self.assertRedirects(response, reverse("game:index"))
        turn.refresh_from_db()
        game.refresh_from_db()
        self.assertIsNotNone(turn.revealed_at)
        self.assertEqual(game.total_score, 1000)
        self.assertTrue(
            Guess.objects.filter(
                turn=turn,
                user__isnull=True,
                guest_key=guest_key,
            ).exists()
        )

    def test_tracking_event_stores_guest_events(self) -> None:
        """Guest players can post tracking events for their current turn."""
        self.create_municipalities(5)
        self.client.post(reverse("game:start"))
        guest_key = self.client.session[GUEST_PLAYER_SESSION_KEY]
        game = Game.objects.get(guest_key=guest_key)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(
            turn,
            payload={"latitude": 47.05, "longitude": 8.05},
        )

        self.assertEqual(response.status_code, 204)
        self.assertTrue(
            GameEvent.objects.filter(
                user__isnull=True,
                guest_key=guest_key,
                game=game,
                turn=turn,
                event_type=GameEvent.Type.MAP_CLICKED,
            ).exists()
        )

    def test_guess_view_shows_result_after_submission(self) -> None:
        """Game index shows the last submitted guess result once."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        first_turn = game.turns.select_related("target").order_by("turn_number").first()
        first_turn.target.population = 12_345
        first_turn.target.save(update_fields=["population"])

        response = self.client.post(
            reverse("game:guess"),
            {
                "turn_id": first_turn.id,
                "latitude": "47.05",
                "longitude": "8.05",
            },
            follow=True,
        )

        self.assertContains(response, "Result")
        self.assertContains(response, first_turn.target.name)
        self.assertContains(response, "Score")
        self.assertContains(response, "1000")
        self.assertContains(response, "Canton")
        self.assertContains(response, "Zurich (ZH)")
        self.assertContains(response, "Population")
        self.assertContains(response, "12345")
        self.assertContains(response, "Distance")
        self.assertNotContains(response, "Boundary")
        self.assertContains(response, "0 m")
        self.assertContains(response, "Next")
        self.assertContains(response, "data-next-turn-link")
        self.assertContains(
            response,
            f'data-tracking-url="{reverse("game:track_turn_event", args=[first_turn.id])}"',
        )
        self.assertContains(
            response,
            (
                f'data-municipality-labels-url="{reverse("geo:municipality_labels_geojson")}'
                f'?turn={first_turn.id}"'
            ),
        )
        self.assertContains(response, 'data-label-min-zoom="11"')
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, f'data-reveal-target-id="{first_turn.target.id}"')
        self.assertContains(response, 'data-reveal-boundary-lat="')
        self.assertContains(response, 'data-reveal-boundary-lng="')
        self.assertNotContains(response, 'data-reveal-boundary-lat=""')
        self.assertNotContains(response, 'data-reveal-boundary-lng=""')
        self.assertContains(response, 'data-reveal-lat="47.050000"')
        self.assertContains(response, 'data-reveal-lng="8.050000"')
        self.assertContains(response, 'data-reveal-distance="0.000000"')
        self.assertNotContains(response, "2/5")
        self.assertNotContains(response, "data-guess-form")

        response = self.client.get(reverse("game:index"))
        self.assertNotContains(response, "Result")
        self.assertContains(response, "2/5")
        self.assertNotIn(
            MUNICIPALITY_LABEL_ACCESS_SESSION_KEY,
            self.client.session,
        )

    def test_guess_view_shows_zero_population_when_present(self) -> None:
        """Game index distinguishes a zero population value from missing data."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        first_turn = game.turns.select_related("target").order_by("turn_number").first()
        first_turn.target.population = 0
        first_turn.target.save(update_fields=["population"])

        response = self.client.post(
            reverse("game:guess"),
            {
                "turn_id": first_turn.id,
                "latitude": "47.05",
                "longitude": "8.05",
            },
            follow=True,
        )

        self.assertContains(response, "Population")
        self.assertContains(response, "<dd>0</dd>", html=True)

    def test_guess_view_shows_final_result_for_finished_game(self) -> None:
        """Final-turn submissions render the finished game result."""
        municipality = self.create_municipalities(1)[0]
        game = Game.objects.create(user=self.user)
        turn = Turn.objects.create(
            game=game,
            turn_number=1,
            target=municipality,
        )
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("game:guess"),
            {
                "turn_id": turn.id,
                "latitude": "47.05",
                "longitude": "8.05",
            },
            follow=True,
        )

        self.assertContains(response, "Finished game")
        self.assertContains(response, "Finished game")
        self.assertContains(response, "Score")
        self.assertContains(response, "Canton")
        self.assertContains(response, "Zurich (ZH)")
        self.assertContains(response, "Summary")
        self.assertContains(response, reverse("game:summary", args=[game.id]))
        self.assertContains(response, 'id="game-map"')
        self.assertContains(
            response,
            (
                f'data-municipality-labels-url="{reverse("geo:municipality_labels_geojson")}'
                f'?turn={turn.id}"'
            ),
        )
        self.assertContains(response, 'data-label-min-zoom="11"')
        self.assertContains(response, f'data-reveal-target-id="{municipality.id}"')
        self.assertContains(response, 'data-reveal-boundary-lat="')
        self.assertContains(response, 'data-reveal-boundary-lng="')
        self.assertNotContains(response, 'data-reveal-boundary-lat=""')
        self.assertNotContains(response, 'data-reveal-boundary-lng=""')
        self.assertContains(response, 'data-reveal-lat="47.050000"')
        self.assertContains(response, 'data-reveal-lng="8.050000"')
        self.assertContains(response, 'data-reveal-distance="0.000000"')
        self.assertNotContains(response, "No active game yet.")
        self.assertNotContains(response, "Start new game")
        self.assertNotContains(response, "data-guess-form")

    def test_guess_view_returns_error_for_invalid_guess(self) -> None:
        """Guess endpoint renders validation errors for invalid submissions."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.client.post(
            reverse("game:guess"),
            {
                "turn_id": turn.id,
                "latitude": "not-a-number",
                "longitude": "8.05",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "game/index.html")
        self.assertContains(response, "Latitude must be a number.", status_code=400)
        self.assertContains(response, 'role="alert"', status_code=400)
        self.assertFalse(Guess.objects.exists())

    def test_game_index_shows_active_game_without_future_targets(self) -> None:
        """Game index shows the current target without revealing future targets."""
        self.create_municipalities(5)
        self.client.force_login(self.user)
        game = start_game(self.user)
        first_turn = game.turns.order_by("turn_number").first()
        future_targets = [
            turn.target.name
            for turn in game.turns.order_by("turn_number")
            if turn.turn_number != 1
        ]

        response = self.client.get(reverse("game:index"))

        self.assertContains(response, "GemeindeGuess CH")
        self.assertContains(response, f"{first_turn.turn_number}/5")
        self.assertContains(response, "Score")
        self.assertNotContains(response, f"Active game #{game.id}")
        self.assertContains(response, first_turn.target.name)
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, "leaflet@1.9.4")
        self.assertContains(
            response,
            "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
        )
        self.assertContains(
            response,
            "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
        )
        self.assertContains(response, 'crossorigin="anonymous"')
        self.assertContains(response, "/static/js/game_map.js")
        self.assertContains(response, 'data-center-lat="46.8182"')
        self.assertContains(response, 'data-map-status')
        self.assertContains(response, 'aria-live="polite"')
        self.assertContains(response, "wmts.geo.admin.ch")
        self.assertContains(response, "ch.swisstopo.swissimage")
        self.assertContains(response, "Guess")
        self.assertNotContains(response, "Place your pin on the map.")
        self.assertNotContains(response, "No point selected")
        self.assertNotContains(response, "Confirm guess")
        self.assertContains(response, reverse("game:guess"))
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="turn_id"')
        self.assertContains(response, f'value="{first_turn.id}"')
        self.assertContains(response, 'name="latitude"')
        self.assertContains(response, 'name="longitude"')
        self.assertContains(response, "data-guess-lat")
        self.assertContains(response, "data-guess-lng")
        self.assertContains(response, "data-confirm-guess")
        self.assertContains(
            response,
            f'data-tracking-url="{reverse("game:track_turn_event", args=[first_turn.id])}"',
        )
        self.assertContains(
            response,
            f'data-canton-boundaries-url="{reverse("geo:cantons_geojson")}"',
        )
        self.assertContains(
            response,
            (
                'data-municipality-boundaries-url="'
                f'{reverse("geo:municipality_boundaries_geojson")}"'
            ),
        )
        self.assertNotContains(response, "data-municipality-labels-url")
        self.assertNotContains(response, "data-label-min-zoom")
        self.assertNotContains(response, reverse("geo:municipality_labels_geojson"))
        for future_target in future_targets:
            self.assertNotContains(response, future_target)

    def test_game_index_handles_active_game_without_turns(self) -> None:
        """Game index reports active games that do not have a current turn."""
        Game.objects.create(user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:index"))

        self.assertContains(response, "No active turn is available")
        self.assertNotContains(response, 'id="game-map"')
        self.assertNotContains(response, "/static/js/game_map.js")

    def test_start_view_returns_error_when_setup_is_incomplete(self) -> None:
        """Game start endpoint reports missing setup data."""
        self.create_municipalities(4)
        self.client.force_login(self.user)

        response = self.client.post(reverse("game:start"))

        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "game/index.html")
        self.assertContains(
            response,
            "At least 5 active municipalities",
            status_code=400,
        )
        self.assertContains(response, 'role="alert"', status_code=400)
        self.assertContains(response, 'aria-live="assertive"', status_code=400)


class GameSummaryTests(TestCase):
    """Tests for finished game summary pages."""

    def setUp(self) -> None:
        """Create shared game summary fixtures."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="player",
            password="StrongPass123!",
        )
        self.other_user = user_model.objects.create_user(
            username="other",
            password="StrongPass123!",
        )
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

    def create_finished_game(
        self,
        user=None,
        guest_key: str = "",
        *,
        bfs_offset: int = 9000,
        canton: Canton | None = None,
        distances: list[float] | None = None,
        finished_at=None,
        mode: str = Game.Mode.SWITZERLAND,
        scores: list[int] | None = None,
    ) -> Game:
        """Create a finished game with five guessed turns.

        Args:
            user: Optional owner for the game.
            guest_key: Optional guest owner for the game.
            bfs_offset: First BFS number to use for generated municipalities.
            canton: Optional canton used for the game and generated targets.
            distances: Optional per-turn municipality distances.
            finished_at: Optional finished timestamp.
            mode: Game mode to store.
            scores: Optional per-turn scores.

        Returns:
            A finished game with five turns and guesses.
        """
        game_user = None if guest_key else user or self.user
        owner_fields = (
            {"user": None, "guest_key": guest_key}
            if guest_key
            else {"user": game_user, "guest_key": ""}
        )
        total_score = 0
        canton = canton or self.canton
        game = Game.objects.create(
            **owner_fields,
            mode=mode,
            canton=canton if mode == Game.Mode.CANTON else None,
            status=Game.Status.FINISHED,
            finished_at=finished_at or timezone.now(),
        )
        for index in range(5):
            score = scores[index] if scores is not None else 1000 - (index * 100)
            distance = distances[index] if distances is not None else index * 1000
            total_score += score
            municipality = Municipality.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=bfs_offset + index,
                name=f"Summary Municipality {index + 1}",
                canton=canton,
                population=10_000 + index,
                geom=make_test_geometry(),
            )
            turn = Turn.objects.create(
                game=game,
                turn_number=index + 1,
                target=municipality,
                revealed_at=timezone.now(),
            )
            Guess.objects.create(
                turn=turn,
                **owner_fields,
                point=Point(8.05, 47.05, srid=4326),
                distance_to_municipality_m=distance,
                distance_to_boundary_m=500 + index,
                nearest_boundary_point=Point(8.0, 47.05, srid=4326),
                score=score,
            )
        game.total_score = total_score
        game.save(update_fields=["total_score"])
        return game

    def test_summary_rejects_anonymous_without_guest_game(self) -> None:
        """Anonymous users need the owning guest key to view guest summaries."""
        game = self.create_finished_game()
        summary_url = reverse("game:summary", args=[game.id])

        response = self.client.get(summary_url)

        self.assertEqual(response.status_code, 404)

    def test_summary_shows_finished_game_results(self) -> None:
        """Summary page shows all turns for a finished owned game."""
        game = self.create_finished_game()
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:summary", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/summary.html")
        self.assertContains(response, "Game result")
        self.assertContains(response, str(game.total_score))
        self.assertContains(response, "Summary Municipality 1")
        self.assertContains(response, "Play again")
        self.assertContains(response, "Change game mode")
        self.assertContains(response, reverse("game:start"))
        self.assertContains(response, reverse("game:index"))
        self.assertContains(response, 'name="game_mode"')
        self.assertContains(response, 'value="switzerland"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, 'data-summary-map="true"')
        self.assertContains(response, "game-summary-reveals")
        self.assertContains(response, "wmts.geo.admin.ch")
        self.assertContains(response, "Canton")
        self.assertContains(response, "ZH")
        self.assertContains(response, "Population")
        self.assertContains(response, "10000")
        self.assertContains(response, "Score")
        self.assertContains(response, "1000")
        self.assertContains(response, "Distance")
        self.assertNotContains(response, "Distance to municipality")
        self.assertNotContains(response, "Distance to boundary")
        self.assertContains(response, "Summary Municipality 5")
        reveals = response.context["summary_reveals"]
        self.assertEqual(len(reveals), 5)
        self.assertEqual([reveal["turnNumber"] for reveal in reveals], [1, 2, 3, 4, 5])
        for index, reveal in enumerate(reveals):
            self.assertEqual(
                set(reveal),
                {
                    "boundaryLat",
                    "boundaryLng",
                    "distance",
                    "lat",
                    "lng",
                    "score",
                    "targetId",
                    "turnNumber",
                },
            )
            self.assertIsInstance(reveal["boundaryLat"], float)
            self.assertIsInstance(reveal["boundaryLng"], float)
            self.assertEqual(reveal["lat"], 47.05)
            self.assertEqual(reveal["lng"], 8.05)
            self.assertEqual(reveal["distance"], index * 1000)
            self.assertEqual(reveal["score"], 1000 - (index * 100))
            self.assertIsInstance(reveal["targetId"], int)
        self.assertContains(response, '"turnNumber": 5')

    def test_summary_uses_canton_game_scope(self) -> None:
        """Summary maps and play-again form preserve single-canton games."""
        game = self.create_finished_game(mode=Game.Mode.CANTON, canton=self.canton)
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:summary", args=[game.id]))

        self.assertContains(
            response,
            (
                'data-municipality-boundaries-url="'
                f'{reverse("geo:municipality_boundaries_geojson")}?canton=ZH"'
            ),
        )
        self.assertContains(response, 'name="game_mode"')
        self.assertContains(response, 'value="canton"')
        self.assertContains(response, 'name="canton"')
        self.assertContains(response, 'value="ZH"')

    def test_build_summary_reveals_uses_stored_boundary_point(self) -> None:
        """Summary reveal payloads reuse persisted nearest boundary points."""
        game = self.create_finished_game()
        summary = get_finished_game_summary(self.user, game.id)

        with patch("game.views.calculate_nearest_boundary_point") as calculate:
            reveals = build_summary_reveals(summary)

        calculate.assert_not_called()
        self.assertEqual(reveals[0]["boundaryLat"], 47.05)
        self.assertEqual(reveals[0]["boundaryLng"], 8.0)

    def test_summary_shows_guest_finished_game_results(self) -> None:
        """Guest players can view summaries for games owned by their guest key."""
        session = self.client.session
        session[GUEST_PLAYER_SESSION_KEY] = "guest-summary-key"
        session.save()
        guest_key = session[GUEST_PLAYER_SESSION_KEY]
        game = self.create_finished_game(guest_key=guest_key)

        response = self.client.get(reverse("game:summary", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Game result")
        self.assertContains(response, str(game.total_score))
        self.assertContains(response, "Summary Municipality 5")

    def test_summary_rejects_other_users_game(self) -> None:
        """Users cannot view another user's game summary."""
        game = self.create_finished_game(user=self.other_user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:summary", args=[game.id]))

        self.assertEqual(response.status_code, 404)

    def test_summary_rejects_active_game(self) -> None:
        """Active game summaries are unavailable to avoid leaking future targets."""
        game = Game.objects.create(user=self.user)
        municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=9900,
            name="Hidden Active Target",
            canton=self.canton,
            geom=make_test_geometry(),
        )
        Turn.objects.create(game=game, turn_number=1, target=municipality)
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:summary", args=[game.id]))

        self.assertEqual(response.status_code, 404)

    def test_history_requires_authenticated_user(self) -> None:
        """History is account-only and redirects anonymous users."""
        response = self.client.get(reverse("game:history"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_history_lists_finished_user_games(self) -> None:
        """History page lists finished games with score and map label."""
        game = self.create_finished_game()
        other_game = Game.objects.create(
            user=self.other_user,
            status=Game.Status.FINISHED,
            total_score=123,
            finished_at=timezone.now(),
        )
        active_game = Game.objects.create(user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:history"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/history.html")
        self.assertContains(response, "History")
        self.assertContains(response, "Personal statistics")
        self.assertContains(response, 'id="game-map"')
        self.assertNotContains(response, 'data-summary-map="true"')
        self.assertContains(response, "Map")
        self.assertContains(response, "CH")
        self.assertContains(response, str(game.total_score))
        self.assertContains(response, reverse("game:history_detail", args=[game.id]))
        self.assertNotContains(
            response,
            reverse("game:history_detail", args=[other_game.id]),
        )
        self.assertNotContains(
            response,
            reverse("game:history_detail", args=[active_game.id]),
        )

    def test_history_shows_empty_state(self) -> None:
        """History page handles users without finished games."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No finished games yet.")
        self.assertContains(response, reverse("game:index"))

    def test_history_detail_shows_selected_game_review(self) -> None:
        """History detail reuses the map summary review for one game."""
        game = self.create_finished_game()
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:history_detail", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/history.html")
        self.assertContains(response, "Game result")
        self.assertContains(response, "Back to history")
        self.assertContains(response, reverse("game:history"))
        self.assertContains(response, "Map")
        self.assertContains(response, "CH")
        self.assertContains(response, 'data-summary-map="true"')
        self.assertContains(response, "game-summary-reveals")
        self.assertContains(response, "Summary Municipality 5")
        self.assertNotContains(response, "New game")
        self.assertEqual(response.context["selected_game"], game)
        self.assertEqual(len(response.context["summary_reveals"]), 5)

    def test_history_detail_rejects_other_users_game(self) -> None:
        """Users cannot review another user's game from history."""
        game = self.create_finished_game(user=self.other_user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:history_detail", args=[game.id]))

        self.assertEqual(response.status_code, 404)

    def test_profile_requires_authenticated_user(self) -> None:
        """Profile statistics are account-only."""
        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_profile_shows_empty_statistics(self) -> None:
        """Profile page renders a clean empty statistics state."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("profile"))
        statistics = response.context["statistics"]

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/profile.html")
        self.assertEqual(statistics["games_played"], 0)
        self.assertEqual(statistics["average_score"], 0)
        self.assertEqual(statistics["best_score"], 0)
        self.assertEqual(statistics["rounds_played"], 0)
        self.assertEqual(statistics["average_distance_m"], 0)
        self.assertEqual(statistics["best_distance_m"], 0)
        self.assertEqual(statistics["perfect_rounds"], 0)
        self.assertEqual(statistics["map_modes"], [])
        self.assertEqual(statistics["recent_games"], [])
        self.assertContains(response, "Games played")
        self.assertContains(response, "Average score")
        self.assertContains(response, "Play a finished game to unlock map stats.")
        self.assertContains(response, "No finished games yet.")
        self.assertNotContains(response, "0 avg score")
        self.assertNotContains(response, "Total score")

    def test_profile_shows_player_statistics_and_recent_games(self) -> None:
        """Profile page summarizes only the signed-in user's finished games."""
        older_game = self.create_finished_game(
            bfs_offset=9100,
            distances=[0, 0.49, 200, 300, 499.51],
            finished_at=timezone.now() - timedelta(days=2),
            scores=[1000, 900, 800, 700, 600],
        )
        newer_game = self.create_finished_game(
            bfs_offset=9200,
            distances=[1000, 2000, 3000, 4000, 5000],
            finished_at=timezone.now() - timedelta(days=1),
            scores=[400, 300, 200, 100, 0],
        )
        self.create_finished_game(user=self.other_user, bfs_offset=9300)
        self.create_finished_game(guest_key="guest-profile-key", bfs_offset=9400)
        Game.objects.create(user=self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("profile"))
        statistics = response.context["statistics"]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(statistics["games_played"], 2)
        self.assertEqual(statistics["average_score"], 2500)
        self.assertEqual(statistics["best_score"], 4000)
        self.assertEqual(statistics["rounds_played"], 10)
        self.assertEqual(statistics["average_distance_m"], 1600)
        self.assertEqual(statistics["best_distance_m"], 0)
        self.assertEqual(statistics["perfect_rounds"], 1)
        self.assertEqual(
            statistics["map_modes"],
            [{"average_score": 2500, "games_played": 2, "label": "CH"}],
        )
        self.assertEqual(statistics["recent_games"], [newer_game, older_game])
        self.assertContains(response, self.user.username)
        self.assertContains(response, "Games played")
        self.assertContains(response, "Average score")
        self.assertContains(response, "Best score")
        self.assertContains(response, "Rounds played")
        self.assertContains(response, "Average distance")
        self.assertContains(response, "1600 m")
        self.assertContains(response, "Best distance")
        self.assertContains(response, "0 m")
        self.assertContains(response, "Perfect rounds")
        self.assertContains(response, "CH")
        self.assertContains(response, "2 games")
        self.assertContains(response, "2500 avg score")
        self.assertContains(
            response,
            reverse("game:history_detail", args=[newer_game.id]),
        )
        self.assertContains(
            response,
            reverse("game:history_detail", args=[older_game.id]),
        )
        self.assertNotContains(response, "Total score")

    def test_build_player_statistics_limits_recent_games(self) -> None:
        """Recent profile games are capped to the latest five finished games."""
        for index in range(6):
            Game.objects.create(
                user=self.user,
                status=Game.Status.FINISHED,
                total_score=index,
                finished_at=timezone.now() + timedelta(minutes=index),
            )

        statistics = build_player_statistics(self.user)

        self.assertEqual(statistics["games_played"], 6)
        self.assertEqual(len(statistics["recent_games"]), 5)
        self.assertEqual(
            [game.total_score for game in statistics["recent_games"]],
            [5, 4, 3, 2, 1],
        )


class GameSelectorTests(TestCase):
    """Tests for game query helpers used by views."""

    def setUp(self) -> None:
        """Create shared fixtures for selector tests."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="player", password="test")
        self.other_user = user_model.objects.create_user(
            username="other",
            password="test",
        )
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
        """Create a municipality fixture for selector tests."""
        return Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=bfs_number,
            name=name,
            canton=self.canton,
            geom=make_test_geometry(),
        )

    def test_get_active_game_returns_current_active_game(self) -> None:
        """Active game selector returns the user's active game and ignores finished ones."""
        Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            finished_at=timezone.now(),
        )
        active_game = Game.objects.create(user=self.user)
        Game.objects.create(user=self.other_user)

        self.assertEqual(get_active_game(self.user), active_game)
        self.assertIsNotNone(get_active_game(self.other_user))

    def test_get_active_game_for_player_returns_guest_game(self) -> None:
        """Active game selector supports guest ownership."""
        active_game = Game.objects.create(user=None, guest_key="guest-session")
        Game.objects.create(user=None, guest_key="other-session")

        self.assertEqual(
            get_active_game_for_player(PlayerIdentity.for_guest("guest-session")),
            active_game,
        )

    def test_get_current_turn_returns_first_unrevealed_turn(self) -> None:
        """Current-turn selector chooses the earliest unrevealed turn."""
        game = Game.objects.create(user=self.user)
        first_target = self.create_municipality(261, "Zurich")
        second_target = self.create_municipality(262, "Winterthur")
        Turn.objects.create(
            game=game,
            turn_number=1,
            target=first_target,
            revealed_at=timezone.now(),
        )
        second_turn = Turn.objects.create(
            game=game,
            turn_number=2,
            target=second_target,
        )

        self.assertEqual(get_current_turn(game), second_turn)
        self.assertIsNone(get_current_turn(None))

    def test_get_finished_games_for_player_returns_newest_owned_games(self) -> None:
        """Finished-game list selector returns only owned finished games."""
        older_game = Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            total_score=100,
            finished_at=timezone.now() - timedelta(days=1),
        )
        newer_game = Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            total_score=200,
            finished_at=timezone.now(),
        )
        Game.objects.create(user=self.user)
        Game.objects.create(
            user=self.other_user,
            status=Game.Status.FINISHED,
            total_score=300,
            finished_at=timezone.now(),
        )

        games = list(get_finished_games_for_player(PlayerIdentity.for_user(self.user)))

        self.assertEqual(games, [newer_game, older_game])

    def test_get_finished_game_summary_returns_ordered_finished_game(self) -> None:
        """Finished-game selector returns ordered turns for the requesting owner."""
        first_target = self.create_municipality(261, "Zurich")
        second_target = self.create_municipality(262, "Winterthur")
        finished_game = Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            finished_at=timezone.now(),
        )
        second_turn = Turn.objects.create(
            game=finished_game,
            turn_number=2,
            target=second_target,
            revealed_at=timezone.now(),
        )
        Guess.objects.create(
            turn=second_turn,
            user=self.user,
            point=Point(8.1, 47.1, srid=4326),
            distance_to_municipality_m=10,
            score=990,
        )
        first_turn = Turn.objects.create(
            game=finished_game,
            turn_number=1,
            target=first_target,
            revealed_at=timezone.now(),
        )
        Guess.objects.create(
            turn=first_turn,
            user=self.user,
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

        summary = get_finished_game_summary(self.user, finished_game.id)

        self.assertIsNotNone(summary)
        self.assertEqual(
            [turn.turn_number for turn in summary.turns.all()],
            [1, 2],
        )
        self.assertEqual(summary.turns.all()[0].guess.score, 1000)
        self.assertIsNone(get_finished_game_summary(self.other_user, finished_game.id))


class GameViewHelperTests(TestCase):
    """Tests for small game view helpers."""

    def setUp(self) -> None:
        """Create shared fixtures for view helper tests."""
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="player", password="test")
        self.other_user = user_model.objects.create_user(
            username="other",
            password="test",
        )
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
        municipality = Municipality.objects.create(
            dataset_version=self.dataset_version,
            bfs_number=261,
            name="Zurich",
            canton=self.canton,
            geom=make_test_geometry(),
        )
        self.game = Game.objects.create(user=self.user)
        self.turn = Turn.objects.create(
            game=self.game,
            turn_number=1,
            target=municipality,
            revealed_at=timezone.now(),
        )
        self.guess = Guess.objects.create(
            turn=self.turn,
            user=self.user,
            point=Point(8.05, 47.05, srid=4326),
            distance_to_municipality_m=0,
            score=1000,
        )

    def test_get_last_guess_result_returns_guess_once(self) -> None:
        """Last-guess helper loads the stored guess and clears last_guess_id."""
        request = RequestFactory().get(reverse("game:index"))
        request.user = self.user
        request.session = {"last_guess_id": str(self.guess.id)}

        result = get_last_guess_result(request)

        self.assertEqual(result, self.guess)
        self.assertNotIn("last_guess_id", request.session)

    def test_get_last_guess_result_returns_guest_guess_once(self) -> None:
        """Last-guess helper supports guest-owned guesses."""
        session = self.client.session
        session[GUEST_PLAYER_SESSION_KEY] = "guest-last-guess-key"
        session.save()
        guest_game = Game.objects.create(
            user=None,
            guest_key=session[GUEST_PLAYER_SESSION_KEY],
        )
        guest_turn = Turn.objects.create(
            game=guest_game,
            turn_number=1,
            target=self.turn.target,
            revealed_at=timezone.now(),
        )
        guest_guess = Guess.objects.create(
            turn=guest_turn,
            user=None,
            guest_key=session[GUEST_PLAYER_SESSION_KEY],
            point=Point(8.06, 47.06, srid=4326),
            distance_to_municipality_m=10,
            score=990,
        )
        request = RequestFactory().get(reverse("game:index"))
        request.user = AnonymousUser()
        request.session = session
        request.session["last_guess_id"] = str(guest_guess.id)

        result = get_last_guess_result(request)

        self.assertEqual(result, guest_guess)
        self.assertNotIn("last_guess_id", request.session)

    def test_get_last_guess_result_ignores_invalid_or_foreign_ids(self) -> None:
        """Last-guess helper ignores invalid ids and guesses owned by others."""
        other_game = Game.objects.create(user=self.other_user)
        other_turn = Turn.objects.create(
            game=other_game,
            turn_number=1,
            target=self.turn.target,
            revealed_at=timezone.now(),
        )
        other_guess = Guess.objects.create(
            turn=other_turn,
            user=self.other_user,
            point=Point(8.06, 47.06, srid=4326),
            distance_to_municipality_m=10,
            score=990,
        )

        for stored_value in ("not-a-number", "0", str(other_guess.id)):
            request = RequestFactory().get(reverse("game:index"))
            request.user = self.user
            request.session = {"last_guess_id": stored_value}

            with self.subTest(stored_value=stored_value):
                self.assertIsNone(get_last_guess_result(request))
                self.assertNotIn("last_guess_id", request.session)
