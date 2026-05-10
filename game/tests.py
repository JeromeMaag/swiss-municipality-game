"""Tests for the game app."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from geo.models import Canton, GeoDatasetVersion, Municipality
from tests.utils import make_test_geometry
from tracking.models import GameEvent

from .models import Game, Guess, Turn
from .scoring import calculate_score
from .services import (
    GuessSubmissionError,
    InvalidGuessCoordinatesError,
    NotEnoughMunicipalitiesError,
    start_game,
    submit_guess,
)
from .views import parse_tracking_request


class ScoringTests(TestCase):
    """Tests for game scoring helpers."""

    def test_calculate_score_returns_maximum_for_exact_hit(self) -> None:
        """An exact hit receives the maximum score."""
        self.assertEqual(calculate_score(0), 1000)

    def test_calculate_score_decays_with_distance(self) -> None:
        """Scores decay according to the configured distance curve."""
        self.assertEqual(calculate_score(5_000), 819)
        self.assertEqual(calculate_score(25_000), 368)
        self.assertEqual(calculate_score(100_000), 18)

    def test_calculate_score_rejects_negative_distance(self) -> None:
        """Negative distances are invalid."""
        with self.assertRaises(ValueError):
            calculate_score(-1)

    def test_calculate_score_rejects_non_finite_distance(self) -> None:
        """Infinite and NaN distances are invalid."""
        for distance in (float("inf"), float("nan")):
            with self.subTest(distance=distance):
                with self.assertRaises(ValueError):
                    calculate_score(distance)


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

    def test_finished_game_requires_finished_at(self) -> None:
        """Finished games require a finish timestamp during validation."""
        game = Game(user=self.user, status=Game.Status.FINISHED)

        with self.assertRaises(ValidationError):
            game.full_clean()

    def test_database_rejects_multiple_active_games_for_same_user(self) -> None:
        """Only one active game can exist per user."""
        Game.objects.create(user=self.user)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Game.objects.create(user=self.user)

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

    def test_submit_guess_scores_outside_polygon_distance(self) -> None:
        """Submitting outside the target polygon stores positive distances."""
        game, turns = self.create_game_with_turns()

        result = submit_guess(self.user, turns[0].id, 47.05, 8.2)

        result.guess.refresh_from_db()
        game.refresh_from_db()
        self.assertGreater(result.guess.distance_to_municipality_m, 0)
        self.assertGreater(result.guess.distance_to_boundary_m, 0)
        self.assertLess(result.guess.score, 1000)
        self.assertGreaterEqual(result.guess.score, 0)
        self.assertEqual(game.total_score, result.guess.score)

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
    ) -> list[Municipality]:
        """Create municipalities for game target selection.

        Args:
            count: Number of municipalities to create.
            is_active: Whether created municipalities are active.

        Returns:
            Created municipality objects.
        """
        municipalities = []
        existing_count = Municipality.objects.filter(
            dataset_version=self.dataset_version
        ).count()
        for index in range(count):
            municipalities.append(
                Municipality.objects.create(
                    dataset_version=self.dataset_version,
                    bfs_number=1000 + existing_count + index,
                    name=f"Municipality {existing_count + index + 1}",
                    canton=self.canton,
                    geom=make_test_geometry(),
                    is_active=is_active,
                )
            )
        return municipalities

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
        self.assertEqual(len(turns), 5)
        self.assertEqual([turn.turn_number for turn in turns], [1, 2, 3, 4, 5])
        self.assertEqual(len({turn.target_id for turn in turns}), 5)
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

    def test_start_game_reuses_existing_active_game(self) -> None:
        """Starting again returns the existing active game."""
        self.create_municipalities(5)

        first_game = start_game(self.user)
        second_game = start_game(self.user)

        self.assertEqual(second_game, first_game)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)
        self.assertEqual(GameEvent.objects.filter(game=first_game).count(), 2)

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
            "game.services.get_active_game",
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
                "game.services.get_active_game",
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
        """Game index renders a start form when no active game exists."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/index.html")
        self.assertContains(response, "No active game yet.")
        self.assertContains(response, reverse("game:start"))
        self.assertNotContains(response, 'id="game-map"')

    def test_start_view_requires_login(self) -> None:
        """Anonymous users cannot start games."""
        response = self.client.post(reverse("game:start"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('game:start')}",
        )

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

    def test_guess_view_requires_login(self) -> None:
        """Anonymous users cannot submit guesses."""
        response = self.client.post(reverse("game:guess"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('game:guess')}",
        )

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

    def test_tracking_event_requires_login(self) -> None:
        """Anonymous users cannot post tracking events."""
        self.create_municipalities(5)
        game = start_game(self.user)
        turn = game.turns.order_by("turn_number").first()

        response = self.post_tracking_event(turn)

        self.assertRedirects(
            response,
            (
                f"{reverse('accounts:login')}?next="
                f"{reverse('game:track_turn_event', args=[turn.id])}"
            ),
        )

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
        self.assertContains(response, "Distance to municipality")
        self.assertContains(response, "0 m")
        self.assertContains(response, "Next turn")
        self.assertContains(response, "data-next-turn-link")
        self.assertContains(
            response,
            f'data-tracking-url="{reverse("game:track_turn_event", args=[first_turn.id])}"',
        )
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, f'data-reveal-target-id="{first_turn.target.id}"')
        self.assertContains(response, 'data-reveal-lat="47.050000"')
        self.assertContains(response, 'data-reveal-lng="8.050000"')
        self.assertNotContains(response, "Turn 2 of 5")
        self.assertNotContains(response, "data-guess-form")

        response = self.client.get(reverse("game:index"))
        self.assertNotContains(response, "Result")
        self.assertContains(response, "Turn 2 of 5")

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
        self.assertContains(response, "Game finished")
        self.assertContains(response, "Result")
        self.assertContains(response, "Total score")
        self.assertContains(response, "Canton")
        self.assertContains(response, "Zurich (ZH)")
        self.assertContains(response, "View summary")
        self.assertContains(response, reverse("game:summary", args=[game.id]))
        self.assertContains(response, 'id="game-map"')
        self.assertContains(response, f'data-reveal-target-id="{municipality.id}"')
        self.assertContains(response, 'data-reveal-lat="47.050000"')
        self.assertContains(response, 'data-reveal-lng="8.050000"')
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

        self.assertContains(response, "Active game")
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
        self.assertContains(response, "No point selected")
        self.assertContains(response, "Confirm guess")
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

    def create_finished_game(self, user=None) -> Game:
        """Create a finished game with five guessed turns.

        Args:
            user: Optional owner for the game.

        Returns:
            A finished game with five turns and guesses.
        """
        game_user = user or self.user
        total_score = 0
        game = Game.objects.create(
            user=game_user,
            status=Game.Status.FINISHED,
            finished_at=timezone.now(),
        )
        for index in range(5):
            score = 1000 - (index * 100)
            total_score += score
            municipality = Municipality.objects.create(
                dataset_version=self.dataset_version,
                bfs_number=9000 + index,
                name=f"Summary Municipality {index + 1}",
                canton=self.canton,
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
                user=game_user,
                point=Point(8.05, 47.05, srid=4326),
                distance_to_municipality_m=index * 1000,
                distance_to_boundary_m=500 + index,
                score=score,
            )
        game.total_score = total_score
        game.save(update_fields=["total_score"])
        return game

    def test_summary_requires_login(self) -> None:
        """Anonymous users cannot view game summaries."""
        game = self.create_finished_game()
        summary_url = reverse("game:summary", args=[game.id])

        response = self.client.get(summary_url)

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={summary_url}",
        )

    def test_summary_shows_finished_game_results(self) -> None:
        """Summary page shows all turns for a finished owned game."""
        game = self.create_finished_game()
        self.client.force_login(self.user)

        response = self.client.get(reverse("game:summary", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "game/summary.html")
        self.assertContains(response, "Game summary")
        self.assertContains(response, str(game.total_score))
        self.assertContains(response, "Summary Municipality 1")
        self.assertContains(response, "Start new game")
        self.assertContains(response, reverse("game:start"))
        self.assertContains(response, 'method="post"')
        self.assertContains(response, "Canton")
        self.assertContains(response, "ZH")
        self.assertContains(response, "Population")
        self.assertContains(response, "10000")
        self.assertContains(response, "Score")
        self.assertContains(response, "1000")
        self.assertContains(response, "Distance to municipality")
        self.assertContains(response, "Turn 5")

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
