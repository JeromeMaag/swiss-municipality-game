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
        on_delete=models.CASCADE,
        related_name="game_events",
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
            models.Index(fields=["event_type", "created_at"]),
        ]

    def __str__(self) -> str:
        """Return the event display label.

        Returns:
            A human-readable event type and user label.
        """
        return f"{self.event_type} for {self.user}"

    def clean(self) -> None:
        """Validate optional event relationships.

        Raises:
            ValidationError: If the linked game, turn, and user do not describe
                the same game owner and game session.
        """
        super().clean()
        errors = {}

        if self.game_id and self.user_id != self.game.user_id:
            errors.setdefault("user", []).append(
                "Event user must match the linked game user."
            )

        if self.turn_id:
            if self.user_id != self.turn.game.user_id:
                errors.setdefault("user", []).append(
                    "Event user must match the linked turn game user."
                )
            if self.game_id and self.turn.game_id != self.game_id:
                errors.setdefault("turn", []).append(
                    "Event turn must belong to the linked game."
                )

        if errors:
            raise ValidationError(errors)
