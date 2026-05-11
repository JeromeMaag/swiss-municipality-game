"""Admin configuration for the tracking app."""

from django.contrib import admin

from .models import GameEvent


@admin.register(GameEvent)
class GameEventAdmin(admin.ModelAdmin):
    """Admin configuration for game events."""

    list_display = ("id", "event_type", "owner_label", "game", "turn", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("event_type", "user__username", "guest_key")
    autocomplete_fields = ("game", "turn")
    readonly_fields = ("user", "guest_key", "created_at")
