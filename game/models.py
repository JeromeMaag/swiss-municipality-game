"""Database models for game sessions and guesses."""

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator


GAME_STATUS_ACTIVE = "active"


class Game(models.Model):
    """A five-turn game session for one player."""

    class Status(models.TextChoices):
        """Allowed lifecycle states for a game."""

        ACTIVE = GAME_STATUS_ACTIVE, "Active"
        FINISHED = "finished", "Finished"
        ABANDONED = "abandoned", "Abandoned"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        blank=True,
        null=True,
        on_delete=models.CASCADE,
        related_name="games",
    )
    guest_key = models.CharField(
        max_length=32,
        blank=True,
        default="",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    total_score = models.PositiveIntegerField(default=0)
    scoring_max_distance_m = models.FloatField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        """Model metadata for games."""

        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(
                fields=["user", "status", "-finished_at", "-id"],
                name="game_user_finished_idx",
            ),
            models.Index(
                fields=["guest_key", "status"],
                name="game_guest_status_idx",
            ),
            models.Index(fields=["status", "started_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(
                    status=GAME_STATUS_ACTIVE,
                    user__isnull=False,
                ),
                name="unique_active_game_per_user",
            ),
            models.UniqueConstraint(
                fields=["guest_key"],
                condition=(
                    models.Q(status=GAME_STATUS_ACTIVE)
                    & ~models.Q(guest_key="")
                ),
                name="unique_active_game_per_guest",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, guest_key="")
                    | (models.Q(user__isnull=True) & ~models.Q(guest_key=""))
                ),
                name="game_owned_by_user_or_guest",
            ),
        ]

    def __str__(self) -> str:
        """Return the game display label.

        Returns:
            A human-readable game label.
        """
        return f"Game {self.pk or 'unsaved'} for {self.owner_label}"

    @property
    def owner_label(self) -> str:
        """Return a compact display label for the game owner."""
        if self.user_id:
            return str(self.user)
        if self.guest_key:
            return f"guest {self.guest_key[:8]}"
        return "unowned player"

    def clean(self) -> None:
        """Validate game lifecycle consistency.

        Raises:
            ValidationError: If a finished game has no finish timestamp.
        """
        super().clean()
        if (self.user_id is None) == (not self.guest_key):
            raise ValidationError(
                "Games must belong to exactly one user or guest."
            )
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
        blank=True,
        editable=False,
        null=True,
        on_delete=models.CASCADE,
        related_name="guesses",
    )
    guest_key = models.CharField(
        max_length=32,
        blank=True,
        default="",
        editable=False,
    )
    point = models.PointField(srid=4326)
    distance_to_municipality_m = models.FloatField(validators=[MinValueValidator(0)])
    distance_to_boundary_m = models.FloatField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0)],
    )
    nearest_boundary_point = models.PointField(
        blank=True,
        editable=False,
        null=True,
        srid=4326,
    )
    score = models.PositiveIntegerField()
    guessed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model metadata for guesses."""

        ordering = ["-guessed_at"]
        verbose_name_plural = "guesses"
        indexes = [
            models.Index(fields=["user", "guessed_at"]),
            models.Index(
                fields=["guest_key", "guessed_at"],
                name="guess_guest_guessed_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, guest_key="")
                    | (models.Q(user__isnull=True) & ~models.Q(guest_key=""))
                ),
                name="guess_owned_by_user_or_guest",
            ),
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

    @property
    def owner_label(self) -> str:
        """Return a compact display label for the guess owner."""
        if self.user_id:
            return str(self.user)
        if self.guest_key:
            return f"guest {self.guest_key[:8]}"
        return "unowned player"

    def sync_owner_from_turn(self) -> None:
        """Derive the guess owner from the linked turn's game."""
        if not self.turn_id:
            return
        game = self.turn.game
        self.user_id = game.user_id
        self.guest_key = game.guest_key

    def save(self, *args, **kwargs) -> None:
        """Persist the guess after syncing owner fields from the game."""
        self.sync_owner_from_turn()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Validate guess consistency.

        Raises:
            ValidationError: If the guess user does not match the game user.
        """
        super().clean()
        errors = {}
        def add_error(field: str, message: str) -> None:
            errors.setdefault(field, []).append(message)

        if (self.user_id is None) == (not self.guest_key):
            add_error("user", "Guesses must belong to exactly one user or guest.")
        if self.turn_id:
            game = self.turn.game
            if self.user_id != game.user_id or self.guest_key != game.guest_key:
                add_error("user", "Guess owner must match the game owner.")
        if errors:
            raise ValidationError(errors)
