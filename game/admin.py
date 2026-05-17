"""Admin configuration for the game app."""

from django.contrib import admin

from .models import Game, Guess, Turn


class TurnInline(admin.TabularInline):
    """Inline admin rows for turns within a game."""

    model = Turn
    extra = 0
    autocomplete_fields = ("municipality_target", "village_target")
    readonly_fields = ("started_at", "revealed_at")


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    """Admin configuration for games."""

    list_display = (
        "id",
        "owner_label",
        "dataset_version",
        "target_type",
        "map_label",
        "status",
        "total_score",
        "started_at",
        "finished_at",
    )
    list_filter = (
        "dataset_version",
        "target_type",
        "mode",
        "canton",
        "status",
        "started_at",
        "finished_at",
    )
    search_fields = ("user__username", "guest_key")
    autocomplete_fields = ("user", "dataset_version", "canton")
    readonly_fields = ("started_at",)
    inlines = (TurnInline,)

    def get_queryset(self, request):
        """Return games with scope data for changelist map labels."""
        return (
            super()
            .get_queryset(request)
            .select_related("dataset_version", "canton")
        )

    def get_readonly_fields(self, request, obj=None):
        """Keep game scope fields read-only once turns have been created."""
        readonly_fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.turns.exists():
            readonly_fields.append("target_type")
            readonly_fields.append("dataset_version")
        return tuple(readonly_fields)


@admin.register(Turn)
class TurnAdmin(admin.ModelAdmin):
    """Admin configuration for turns."""

    list_display = (
        "id",
        "game",
        "turn_number",
        "municipality_target",
        "village_target",
        "started_at",
        "revealed_at",
    )
    list_filter = ("turn_number", "started_at", "revealed_at")
    search_fields = ("municipality_target__name", "village_target__name")
    autocomplete_fields = ("game", "municipality_target", "village_target")
    readonly_fields = ("started_at",)


@admin.register(Guess)
class GuessAdmin(admin.ModelAdmin):
    """Admin configuration for guesses."""

    list_display = (
        "id",
        "turn",
        "owner_label",
        "distance_to_municipality_m",
        "score",
        "guessed_at",
    )
    list_filter = ("guessed_at",)
    search_fields = (
        "user__username",
        "guest_key",
        "turn__municipality_target__name",
        "turn__village_target__name",
    )
    autocomplete_fields = ("turn",)
    readonly_fields = ("user", "guest_key", "guessed_at")
