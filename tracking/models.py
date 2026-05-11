"""Database models for persisted game events."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class GameEvent(models.Model):
    """A persisted event emitted during user and game flows."""

    class Type(models.TextChoices):
        """Known event type values."""

        USER_REGISTERED = "USER_REGISTERED", "User registered"
        USER_LOGGED_IN = "USER_LOGGED_IN", "User logged in"
        GAME_STARTED = "GAME_STARTED", "Game started"
        TURN_STARTED = "TURN_STARTED", "Turn started"
        MAP_CLICKED = "MAP_CLICKED", "Map clicked"
        PIN_MOVED = "PIN_MOVED", "Pin moved"
        GUESS_CONFIRMED = "GUESS_CONFIRMED", "Guess confirmed"
        REVEAL_SHOWN = "REVEAL_SHOWN", "Reveal shown"
        NEXT_TURN_CLICKED = "NEXT_TURN_CLICKED", "Next turn clicked"
        GAME_FINISHED = "GAME_FINISHED", "Game finished"
        GAME_ABANDONED = "GAME_ABANDONED", "Game abandoned"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        editable=False,
        null=True,
        on_delete=models.CASCADE,
        related_name="game_events",
    )
    guest_key = models.CharField(
        max_length=32,
        blank=True,
        default="",
        editable=False,
    )
    game = models.ForeignKey(
        "game.Game",
        on_delete=models.CASCADE,
        related_name="events",
        blank=True,
        null=True,
    )
    turn = models.ForeignKey(
        "game.Turn",
        on_delete=models.CASCADE,
        related_name="events",
        blank=True,
        null=True,
    )
    event_type = models.CharField(max_length=40, choices=Type.choices)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata for game events."""

        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(
                fields=["guest_key", "created_at"],
                name="event_guest_created_idx",
            ),
            models.Index(fields=["event_type", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, guest_key="")
                    | (models.Q(user__isnull=True) & ~models.Q(guest_key=""))
                ),
                name="event_owned_by_user_or_guest",
            ),
        ]

    def __str__(self) -> str:
        """Return the event display label.

        Returns:
            A human-readable event type and owner label.
        """
        return f"{self.event_type} for {self.owner_label}"

    @property
    def owner_label(self) -> str:
        """Return a compact display label for the event owner."""
        if self.user_id:
            return str(self.user)
        if self.guest_key:
            return f"guest {self.guest_key[:8]}"
        return "unowned player"

    def sync_owner_from_relationships(self) -> None:
        """Derive the event owner from linked turn or game relationships."""
        game = None
        if self.turn_id:
            game = self.turn.game
        elif self.game_id:
            game = self.game
        if game is None:
            return
        self.user_id = game.user_id
        self.guest_key = game.guest_key

    def save(self, *args, **kwargs) -> None:
        """Persist the event after syncing owner fields from game context."""
        self.sync_owner_from_relationships()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Validate optional event relationships.

        Raises:
            ValidationError: If the linked game, turn, and user do not describe
                the same game owner and game relationship.
        """
        super().clean()
        errors = {}
        if (self.user_id is None) == (not self.guest_key):
            errors.setdefault("user", []).append(
                "Event must belong to exactly one user or guest."
            )

        if self.game_id and (
            self.user_id != self.game.user_id
            or self.guest_key != self.game.guest_key
        ):
            errors.setdefault("user", []).append(
                "Event owner must match the linked game owner."
            )

        if self.turn_id:
            if (
                self.user_id != self.turn.game.user_id
                or self.guest_key != self.turn.game.guest_key
            ):
                errors.setdefault("user", []).append(
                    "Event owner must match the linked turn game owner."
                )
            if self.game_id and self.turn.game_id != self.game_id:
                errors.setdefault("turn", []).append(
                    "Event turn must belong to the linked game."
                )

        if errors:
            raise ValidationError(errors)
