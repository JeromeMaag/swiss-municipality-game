"""Database models for game sessions and guesses."""

import math

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils.translation import gettext_lazy as _


GAME_STATUS_ACTIVE = "active"
GAME_MODE_SWITZERLAND = "switzerland"
GAME_MODE_CANTON = "canton"
GAME_TARGET_TYPE_MUNICIPALITY = "municipality"
GAME_TARGET_TYPE_VILLAGE = "village"


class Game(models.Model):
    """A five-turn game session for one player."""

    class Status(models.TextChoices):
        """Allowed lifecycle states for a game."""

        ACTIVE = GAME_STATUS_ACTIVE, "Active"
        FINISHED = "finished", "Finished"
        ABANDONED = "abandoned", "Abandoned"

    class Mode(models.TextChoices):
        """Allowed map scopes for a game."""

        SWITZERLAND = GAME_MODE_SWITZERLAND, "Switzerland"
        CANTON = GAME_MODE_CANTON, "Single canton"

    class TargetType(models.TextChoices):
        """Allowed geographic target types for a game."""

        MUNICIPALITY = GAME_TARGET_TYPE_MUNICIPALITY, "Municipality"
        VILLAGE = GAME_TARGET_TYPE_VILLAGE, "Village"

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
    mode = models.CharField(
        max_length=20,
        choices=Mode.choices,
        default=Mode.SWITZERLAND,
    )
    target_type = models.CharField(
        max_length=20,
        choices=TargetType.choices,
        default=TargetType.MUNICIPALITY,
    )
    dataset_version = models.ForeignKey(
        "geo.GeoDatasetVersion",
        blank=True,
        on_delete=models.PROTECT,
        related_name="games",
    )
    canton = models.ForeignKey(
        "geo.Canton",
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="games",
    )
    total_score = models.PositiveIntegerField(default=0)
    scoring_max_distance_m = models.FloatField(
        blank=True,
        null=True,
        validators=[MinValueValidator(0.000001)],
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
            models.Index(fields=["mode", "canton"], name="game_mode_canton_idx"),
            models.Index(
                fields=["target_type", "mode", "canton"],
                name="game_target_scope_idx",
            ),
            models.Index(
                fields=["dataset_version", "status"],
                name="game_dataset_status_idx",
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
            models.CheckConstraint(
                condition=(
                    models.Q(scoring_max_distance_m__isnull=True)
                    | (
                        models.Q(scoring_max_distance_m__gt=0)
                        & models.Q(scoring_max_distance_m__lt=float("inf"))
                    )
                ),
                name="game_scoring_max_distance_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(mode=GAME_MODE_SWITZERLAND, canton__isnull=True)
                    | models.Q(mode=GAME_MODE_CANTON, canton__isnull=False)
                ),
                name="game_mode_canton_consistency",
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

    @property
    def map_label(self) -> str:
        """Return the compact map scope label."""
        if self.mode == self.Mode.CANTON and self.canton_id:
            return self.canton.abbreviation
        return "CH"

    @property
    def target_type_label(self) -> str:
        """Return the plural target type label for game UI surfaces."""
        if self.target_type == self.TargetType.VILLAGE:
            return _("Villages")
        return _("Municipalities")

    def clean(self) -> None:
        """Validate game ownership, scope, target type, and lifecycle consistency.

        Raises:
            ValidationError: If ownership, scope, scoring extent, or
                finished-game lifecycle data is invalid.
        """
        super().clean()
        if (self.user_id is None) == (not self.guest_key):
            raise ValidationError(
                "Games must belong to exactly one user or guest."
            )
        if self.mode == self.Mode.SWITZERLAND and self.canton_id is not None:
            raise ValidationError(
                {"canton": "Switzerland games must not store a canton."}
            )
        if self.mode == self.Mode.CANTON and self.canton_id is None:
            raise ValidationError(
                {"canton": "Single-canton games require a canton."}
            )
        self.validate_dataset_scope()
        if (
            self.scoring_max_distance_m is not None
            and not math.isfinite(self.scoring_max_distance_m)
        ):
            raise ValidationError(
                {"scoring_max_distance_m": "Scoring map extent must be finite."}
            )
        if self.status == self.Status.FINISHED and self.finished_at is None:
            raise ValidationError(
                {"finished_at": "Finished games require a finish timestamp."}
            )
        if self.dataset_version_changed_after_turns_exist():
            raise ValidationError(
                {
                    "dataset_version": (
                        "Game dataset version cannot change after turns have "
                        "been created."
                    )
                }
            )
        if self.target_type_changed_after_turns_exist():
            raise ValidationError(
                {
                    "target_type": (
                        "Game target type cannot change after turns have been "
                        "created."
                    )
                }
            )

    def save(self, *args, **kwargs) -> None:
        """Persist the game while preserving existing turn target consistency."""
        if self.dataset_version_id is None:
            from geo.selectors import get_current_dataset_version

            current_dataset_version = get_current_dataset_version()
            if current_dataset_version is None:
                raise ValidationError(
                    {
                        "dataset_version": (
                            "A geodata dataset version is required to create "
                            "a game."
                        )
                    }
                )
            self.dataset_version = current_dataset_version
        self.validate_dataset_scope()
        if self.target_type_changed_after_turns_exist(
            update_fields=kwargs.get("update_fields"),
        ):
            raise ValidationError(
                {
                    "target_type": (
                        "Game target type cannot change after turns have been "
                        "created."
                    )
                }
            )
        if self.dataset_version_changed_after_turns_exist(
            update_fields=kwargs.get("update_fields"),
        ):
            raise ValidationError(
                {
                    "dataset_version": (
                        "Game dataset version cannot change after turns have "
                        "been created."
                    )
                }
            )
        super().save(*args, **kwargs)

    def validate_dataset_scope(self) -> None:
        """Validate that stored game scope belongs to the game dataset."""
        if (
            self.canton_id
            and self.dataset_version_id
            and self.canton.dataset_version_id != self.dataset_version_id
        ):
            raise ValidationError(
                {
                    "canton": (
                        "Game canton must belong to the game's dataset version."
                    )
                }
            )

    def target_type_changed_after_turns_exist(self, *, update_fields=None) -> bool:
        """Return whether target type was changed after turns were created."""
        if self.pk is None:
            return False
        if update_fields is not None and "target_type" not in update_fields:
            return False
        persisted_target_type = (
            type(self).objects.filter(pk=self.pk)
            .values_list("target_type", flat=True)
            .first()
        )
        return (
            persisted_target_type is not None
            and persisted_target_type != self.target_type
            and self.turns.exists()
        )

    def dataset_version_changed_after_turns_exist(self, *, update_fields=None) -> bool:
        """Return whether dataset version changed after turns were created."""
        if self.pk is None:
            return False
        if update_fields is not None and not {
            "dataset_version",
            "dataset_version_id",
        }.intersection(update_fields):
            return False
        persisted_dataset_version_id = (
            type(self).objects.filter(pk=self.pk)
            .values_list("dataset_version_id", flat=True)
            .first()
        )
        return (
            persisted_dataset_version_id is not None
            and persisted_dataset_version_id != self.dataset_version_id
            and self.turns.exists()
        )


class Turn(models.Model):
    """One target within a game."""

    game = models.ForeignKey(
        Game,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    turn_number = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    municipality_target = models.ForeignKey(
        "geo.Municipality",
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="target_turns",
    )
    village_target = models.ForeignKey(
        "geo.Village",
        blank=True,
        null=True,
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
                fields=["game", "municipality_target"],
                condition=models.Q(municipality_target__isnull=False),
                name="unique_turn_municipality_target_per_game",
            ),
            models.UniqueConstraint(
                fields=["game", "village_target"],
                condition=models.Q(village_target__isnull=False),
                name="unique_turn_village_target_per_game",
            ),
            models.CheckConstraint(
                condition=models.Q(turn_number__gte=1, turn_number__lte=5),
                name="turn_number_between_1_and_5",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        municipality_target__isnull=False,
                        village_target__isnull=True,
                    )
                    | models.Q(
                        municipality_target__isnull=True,
                        village_target__isnull=False,
                    )
                ),
                name="turn_has_exactly_one_target",
            ),
        ]

    def __str__(self) -> str:
        """Return the turn display label.

        Returns:
            A human-readable turn label.
        """
        return f"Turn {self.turn_number} of game {self.game_id or 'unsaved'}"

    @property
    def selected_target(self):
        """Return the municipality or village target matching the game type."""
        if self.game.target_type == Game.TargetType.VILLAGE:
            return self.village_target
        return self.municipality_target

    @property
    def selected_target_name(self) -> str:
        """Return the display name for the selected turn target."""
        target = self.selected_target
        return target.name if target is not None else ""

    @property
    def selected_target_canton(self):
        """Return the canton for the selected turn target."""
        target = self.selected_target
        return target.canton if target is not None else None

    @property
    def selected_target_population(self) -> int | None:
        """Return target population when the selected target stores it."""
        target = self.selected_target
        return getattr(target, "population", None)

    def clean(self) -> None:
        """Validate turn consistency.

        Raises:
            ValidationError: If target ownership or activity is invalid.
        """
        super().clean()
        errors: dict[str, list[str]] = {}
        has_municipality = self.municipality_target_id is not None
        has_village = self.village_target_id is not None

        if has_municipality == has_village:
            errors.setdefault("municipality_target", []).append(
                "Turns must have exactly one municipality or village target."
            )
        if (
            self.game_id
            and self.game.target_type == Game.TargetType.MUNICIPALITY
            and not has_municipality
        ):
            errors.setdefault("municipality_target", []).append(
                "Municipality games require a municipality target."
            )
        if (
            self.game_id
            and self.game.target_type == Game.TargetType.VILLAGE
            and not has_village
        ):
            errors.setdefault("village_target", []).append(
                "Village games require a village target."
            )
        if self.municipality_target_id and not self.municipality_target.is_active:
            errors.setdefault("municipality_target", []).append(
                "Target municipality must be active."
            )
        if self.village_target_id and not self.village_target.is_active:
            errors.setdefault("village_target", []).append(
                "Target village must be active."
            )
        if (
            self.game_id
            and self.game.mode == Game.Mode.CANTON
            and self.game.canton_id is not None
        ):
            if (
                self.municipality_target_id
                and self.municipality_target.canton_id != self.game.canton_id
            ):
                errors.setdefault("municipality_target", []).append(
                    "Turn target must belong to the game's canton."
                )
            if (
                self.village_target_id
                and self.village_target.canton_id != self.game.canton_id
            ):
                errors.setdefault("village_target", []).append(
                    "Turn target must belong to the game's canton."
                )
        if self.game_id and self.game.dataset_version_id is not None:
            if (
                self.municipality_target_id
                and self.municipality_target.dataset_version_id
                != self.game.dataset_version_id
            ):
                errors.setdefault("municipality_target", []).append(
                    "Turn target must belong to the game's dataset version."
                )
            if (
                self.village_target_id
                and self.village_target.dataset_version_id
                != self.game.dataset_version_id
            ):
                errors.setdefault("village_target", []).append(
                    "Turn target must belong to the game's dataset version."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs) -> None:
        """Persist the turn after enforcing target consistency rules."""
        update_fields = kwargs.get("update_fields")
        target_fields = {
            "game",
            "game_id",
            "municipality_target",
            "municipality_target_id",
            "village_target",
            "village_target_id",
        }
        if (
            self._state.adding
            or update_fields is None
            or target_fields.intersection(update_fields)
        ):
            self.clean()
        super().save(*args, **kwargs)


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
            ValidationError: If the guess owner does not match the game owner.
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
