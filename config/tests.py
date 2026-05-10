"""Tests for project-level helpers and views."""

import os
from unittest import mock

from django.test import SimpleTestCase
from django.urls import reverse

from .settings import get_bool_env, get_list_env


class SettingsHelperTests(SimpleTestCase):
    """Tests for small environment parsing helpers."""

    def test_get_bool_env_uses_default_when_missing(self) -> None:
        """Missing boolean env vars fall back to the provided default."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(get_bool_env("FEATURE_FLAG", default=True))
            self.assertFalse(get_bool_env("FEATURE_FLAG", default=False))

    def test_get_bool_env_accepts_truthy_values_case_insensitively(self) -> None:
        """Boolean helper recognizes supported truthy string values."""
        truthy_values = ("1", "true", "yes", "on", "TrUe")

        for value in truthy_values:
            with self.subTest(value=value), mock.patch.dict(
                os.environ,
                {"FEATURE_FLAG": value},
                clear=True,
            ):
                self.assertTrue(get_bool_env("FEATURE_FLAG"))

    def test_get_list_env_trims_values_and_drops_empty_items(self) -> None:
        """List helper splits comma-separated env vars into clean values."""
        with mock.patch.dict(
            os.environ,
            {"ALLOWED_HOSTS": " localhost, ,example.test,127.0.0.1 "},
            clear=True,
        ):
            self.assertEqual(
                get_list_env("ALLOWED_HOSTS"),
                ["localhost", "example.test", "127.0.0.1"],
            )


class HomeViewTests(SimpleTestCase):
    """Tests for the public home page view."""

    def test_home_renders(self) -> None:
        """Home page responds successfully to GET requests."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")

    def test_home_rejects_post_requests(self) -> None:
        """Home page only allows GET requests."""
        response = self.client.post(reverse("home"))

        self.assertEqual(response.status_code, 405)
