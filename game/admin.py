"""Admin configuration for the game app."""

from django.contrib import admin

from .models import Game, Guess, Turn


class TurnInline(admin.TabularInline):
    """Inline admin rows for turns within a game."""

    model = Turn
    extra = 0
    autocomplete_fields = ("target",)
    readonly_fields = ("started_at", "revealed_at")


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    """Admin configuration for games."""

    list_display = (
        "id",
        "owner_label",
        "map_label",
        "status",
        "total_score",
        "started_at",
        "finished_at",
    )
    list_filter = ("mode", "canton", "status", "started_at", "finished_at")
    search_fields = ("user__username", "guest_key")
    autocomplete_fields = ("user", "canton")
    readonly_fields = ("started_at",)
    inlines = (TurnInline,)

    def get_queryset(self, request):
        """Return games with canton data for changelist map labels."""
        return super().get_queryset(request).select_related("canton")


@admin.register(Turn)
class TurnAdmin(admin.ModelAdmin):
    """Admin configuration for turns."""

    list_display = ("id", "game", "turn_number", "target", "started_at", "revealed_at")
    list_filter = ("turn_number", "started_at", "revealed_at")
    search_fields = ("target__name",)
    autocomplete_fields = ("game", "target")
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
    search_fields = ("user__username", "guest_key", "turn__target__name")
    autocomplete_fields = ("turn",)
    readonly_fields = ("user", "guest_key", "guessed_at")
