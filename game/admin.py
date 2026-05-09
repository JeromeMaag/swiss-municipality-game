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

    list_display = ("id", "user", "status", "total_score", "started_at", "finished_at")
    list_filter = ("status", "started_at", "finished_at")
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)
    readonly_fields = ("started_at",)
    inlines = (TurnInline,)


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
        "user",
        "distance_to_municipality_m",
        "score",
        "guessed_at",
    )
    list_filter = ("guessed_at",)
    search_fields = ("user__username", "turn__target__name")
    autocomplete_fields = ("turn", "user")
    readonly_fields = ("guessed_at",)
