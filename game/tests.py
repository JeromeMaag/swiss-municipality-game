"""Tests for the game app."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from geo.models import Canton, GeoDatasetVersion, Municipality
from tests.utils import make_test_geometry
from tracking.models import GameEvent

from .models import Game, Guess, Turn
from .services import NotEnoughMunicipalitiesError, start_game


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

    def test_start_view_creates_game_and_redirects(self) -> None:
        """Game start endpoint creates a game and redirects to the index."""
        self.create_municipalities(5)
        self.client.force_login(self.user)

        response = self.client.post(reverse("game:start"))

        self.assertRedirects(response, reverse("game:index"))
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Turn.objects.count(), 5)

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
        self.assertContains(response, "/static/js/game_map.js")
        self.assertContains(response, 'data-center-lat="46.8182"')
        self.assertContains(response, "No point selected")
        self.assertContains(response, "Confirm guess")
        self.assertContains(response, "data-guess-lat")
        self.assertContains(response, "data-guess-lng")
        self.assertContains(response, "data-confirm-guess")
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
