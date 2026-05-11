"""Tests for the tracking app."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from game.models import Game, Turn
from geo.models import Canton, GeoDatasetVersion, Municipality
from tests.utils import make_test_geometry

from .models import GameEvent
from .services import track_event


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

    def test_game_event_accepts_guest_owner(self) -> None:
        """Game events can belong to a guest."""
        guest_game = Game.objects.create(user=None, guest_key="guest-session")
        event = GameEvent(
            user=None,
            guest_key="guest-session",
            game=guest_game,
            event_type=GameEvent.Type.GAME_STARTED,
        )

        event.full_clean()

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
        other_game = Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            finished_at=timezone.now(),
        )
        event = GameEvent(
            user=self.user,
            game=other_game,
            turn=self.turn,
            event_type=GameEvent.Type.TURN_STARTED,
        )

        with self.assertRaises(ValidationError):
            event.full_clean()

    def test_event_requires_exactly_one_owner(self) -> None:
        """Event validation rejects missing and mixed owners."""
        invalid_events = [
            GameEvent(event_type=GameEvent.Type.GAME_STARTED),
            GameEvent(
                user=self.user,
                guest_key="guest-session",
                event_type=GameEvent.Type.GAME_STARTED,
            ),
        ]

        for event in invalid_events:
            with self.subTest(user=event.user, guest_key=event.guest_key):
                with self.assertRaises(ValidationError):
                    event.full_clean()

    def test_event_save_derives_user_owner_from_game(self) -> None:
        """Direct event saves sync user ownership from the linked game."""
        event = GameEvent.objects.create(
            user=self.other_user,
            guest_key="wrong-guest",
            game=self.game,
            event_type=GameEvent.Type.GAME_STARTED,
        )

        event.refresh_from_db()
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.guest_key, "")

    def test_event_save_derives_user_owner_from_turn(self) -> None:
        """Direct event saves sync user ownership from the linked turn game."""
        event = GameEvent.objects.create(
            user=self.other_user,
            guest_key="wrong-guest",
            turn=self.turn,
            event_type=GameEvent.Type.TURN_STARTED,
        )

        event.refresh_from_db()
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.guest_key, "")

    def test_event_save_derives_guest_owner_from_game(self) -> None:
        """Direct event saves sync guest ownership from the linked game."""
        guest_game = Game.objects.create(user=None, guest_key="guest-session")
        event = GameEvent.objects.create(
            user=self.user,
            guest_key="wrong-guest",
            game=guest_game,
            event_type=GameEvent.Type.GAME_STARTED,
        )

        event.refresh_from_db()
        self.assertIsNone(event.user_id)
        self.assertEqual(event.guest_key, "guest-session")

    def test_event_save_prefers_turn_owner_when_game_also_linked(self) -> None:
        """Turn ownership wins when game and turn are both supplied."""
        other_game = Game.objects.create(
            user=self.user,
            status=Game.Status.FINISHED,
            finished_at=timezone.now(),
        )
        guest_game = Game.objects.create(user=None, guest_key="guest-session")
        guest_turn = Turn.objects.create(
            game=guest_game,
            turn_number=1,
            target=self.municipality,
        )

        event = GameEvent.objects.create(
            user=self.user,
            game=other_game,
            turn=guest_turn,
            event_type=GameEvent.Type.TURN_STARTED,
        )

        event.refresh_from_db()
        self.assertIsNone(event.user_id)
        self.assertEqual(event.guest_key, "guest-session")


class TrackingServiceTests(TestCase):
    """Tests for tracking event persistence helpers."""

    def setUp(self) -> None:
        """Create shared fixtures for tracking service tests."""
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

    def test_track_event_persists_default_payload(self) -> None:
        """Tracking helper stores an empty payload when none is provided."""
        event = track_event(
            user=self.user,
            game=self.game,
            turn=self.turn,
            event_type=GameEvent.Type.TURN_STARTED,
        )

        self.assertEqual(event.payload, {})
        self.assertTrue(
            GameEvent.objects.filter(
                user=self.user,
                game=self.game,
                turn=self.turn,
                event_type=GameEvent.Type.TURN_STARTED,
            ).exists()
        )

    def test_track_event_accepts_guest_owner(self) -> None:
        """Tracking helper persists events for guest owners."""
        guest_game = Game.objects.create(user=None, guest_key="guest-session")

        event = track_event(
            guest_key="guest-session",
            game=guest_game,
            event_type=GameEvent.Type.GAME_STARTED,
        )

        self.assertIsNone(event.user_id)
        self.assertEqual(event.guest_key, "guest-session")
        self.assertTrue(
            GameEvent.objects.filter(
                pk=event.pk,
                guest_key="guest-session",
                event_type=GameEvent.Type.GAME_STARTED,
            ).exists()
        )

    def test_track_event_validates_relationships_before_saving(self) -> None:
        """Tracking helper rejects invalid relationships without persisting data."""
        with self.assertRaises(ValidationError):
            track_event(
                user=self.other_user,
                game=self.game,
                turn=self.turn,
                event_type=GameEvent.Type.TURN_STARTED,
            )

        self.assertEqual(GameEvent.objects.count(), 0)
