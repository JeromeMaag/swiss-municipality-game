"""Application configuration for the accounts app."""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Configure the accounts app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
