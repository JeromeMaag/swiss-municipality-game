"""Application configuration for the tracking app."""

from django.apps import AppConfig


class TrackingConfig(AppConfig):
    """Configure the tracking app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "tracking"
