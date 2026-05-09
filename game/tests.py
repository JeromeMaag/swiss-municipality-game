"""Tests for the game app."""

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from geo.models import Canton, GeoDatasetVersion, Municipality

from .models import Game, Guess, Turn


def make_test_geometry() -> MultiPolygon:
    """Create a simple WGS84 multipolygon for game model tests.

    Returns:
        A square multipolygon with SRID 4326.
    """
    polygon = Polygon(
        ((8.0, 47.0), (8.1, 47.0), (8.1, 47.1), (8.0, 47.1), (8.0, 47.0))
    )
    return MultiPolygon(polygon, srid=4326)


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
