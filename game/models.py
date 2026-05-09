"""Database models for game sessions and guesses."""

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator


class Game(models.Model):
    """A five-turn game session for one user."""

    class Status(models.TextChoices):
        """Allowed lifecycle states for a game."""

        ACTIVE = "active", "Active"
        FINISHED = "finished", "Finished"
        ABANDONED = "abandoned", "Abandoned"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="games",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    total_score = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """Model metadata for games."""

        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "started_at"]),
        ]

    def __str__(self) -> str:
        """Return the game display label.

        Returns:
            A human-readable game label.
        """
        return f"Game {self.pk or 'unsaved'} for {self.user}"

    def clean(self) -> None:
        """Validate game lifecycle consistency.

        Raises:
            ValidationError: If a finished game has no finish timestamp.
        """
        super().clean()
        if self.status == self.Status.FINISHED and self.finished_at is None:
            raise ValidationError(
                {"finished_at": "Finished games require a finish timestamp."}
            )


class Turn(models.Model):
    """One target municipality within a game."""

    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    turn_number = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    target = models.ForeignKey(
        "geo.Municipality",
        on_delete=models.PROTECT,
        related_name="target_turns",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    revealed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """Model metadata for turns."""

        ordering = ["game", "turn_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "turn_number"],
                name="unique_turn_number_per_game",
            ),
            models.UniqueConstraint(
                fields=["game", "target"],
                name="unique_turn_target_per_game",
            ),
            models.CheckConstraint(
                condition=models.Q(turn_number__gte=1, turn_number__lte=5),
                name="turn_number_between_1_and_5",
            ),
        ]

    def __str__(self) -> str:
        """Return the turn display label.

        Returns:
            A human-readable turn label.
        """
        return f"Turn {self.turn_number} of game {self.game_id or 'unsaved'}"

    def clean(self) -> None:
        """Validate turn consistency.

        Raises:
            ValidationError: If the target municipality is inactive.
        """
        super().clean()
        if self.target_id and not self.target.is_active:
            raise ValidationError({"target": "Target municipality must be active."})


class Guess(models.Model):
    """A user's submitted point guess for one turn."""

    turn = models.OneToOneField(
        Turn,
        on_delete=models.CASCADE,
        related_name="guess",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="guesses",
    )
    point = models.PointField(srid=4326)
    distance_to_municipality_m = models.FloatField(validators=[MinValueValidator(0)])
    distance_to_boundary_m = models.FloatField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
    )
    score = models.PositiveIntegerField()
    guessed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata for guesses."""

        ordering = ["-guessed_at"]
        verbose_name_plural = "guesses"
        indexes = [
            models.Index(fields=["user", "guessed_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(distance_to_municipality_m__gte=0),
                name="guess_municipality_distance_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(distance_to_boundary_m__isnull=True)
                    | models.Q(distance_to_boundary_m__gte=0)
                ),
                name="guess_boundary_distance_non_negative",
            ),
        ]

    def __str__(self) -> str:
        """Return the guess display label.

        Returns:
            A human-readable guess label.
        """
        return f"Guess for {self.turn}"

    def clean(self) -> None:
        """Validate guess consistency.

        Raises:
            ValidationError: If the guess user does not match the game user.
        """
        super().clean()
        if self.turn_id and self.user_id != self.turn.game.user_id:
            raise ValidationError({"user": "Guess user must match the game user."})
