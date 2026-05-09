"""Tests for the tracking app."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from game.models import Game, Turn
from geo.models import Canton, GeoDatasetVersion, Municipality
from tests.utils import make_test_geometry

from .models import GameEvent


class GameEventModelTests(TestCase):
    """Tests for game event model behavior."""

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
        self.game = Game.objects.create(user=self.user)
        self.turn = Turn.objects.create(
            game=self.game,
            turn_number=1,
            target=self.municipality,
        )

    def test_game_event_stores_json_payload(self) -> None:
        """Game events persist arbitrary JSON payload data."""
        event = GameEvent.objects.create(
            user=self.user,
            game=self.game,
            turn=self.turn,
            event_type=GameEvent.Type.MAP_CLICKED,
            payload={"lat": 47.05, "lng": 8.05},
        )

        self.assertEqual(event.payload["lat"], 47.05)
        self.assertEqual(str(event), "MAP_CLICKED for player")

    def test_event_user_must_match_game_user(self) -> None:
        """Event validation rejects users that do not own the linked game."""
        event = GameEvent(
            user=self.other_user,
            game=self.game,
            event_type=GameEvent.Type.GAME_STARTED,
        )

        with self.assertRaises(ValidationError):
            event.full_clean()

    def test_event_user_must_match_turn_game_user(self) -> None:
        """Event validation rejects users that do not own the linked turn game."""
        event = GameEvent(
            user=self.other_user,
            turn=self.turn,
            event_type=GameEvent.Type.TURN_STARTED,
        )

        with self.assertRaises(ValidationError):
            event.full_clean()

    def test_event_turn_must_belong_to_game(self) -> None:
        """Event validation rejects turns from another linked game."""
        other_game = Game.objects.create(user=self.user)
        event = GameEvent(
            user=self.user,
            game=other_game,
            turn=self.turn,
            event_type=GameEvent.Type.TURN_STARTED,
        )

        with self.assertRaises(ValidationError):
            event.full_clean()
