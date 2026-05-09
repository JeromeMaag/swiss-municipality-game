"""Application configuration for the geo app."""

from django.apps import AppConfig


class GeoConfig(AppConfig):
    """Configure the geo app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "geo"
