"""Tests for project-level helpers and views."""

import os
import subprocess
import sys
from unittest import mock

from django.conf import settings
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

    def test_get_bool_env_treats_unknown_values_as_false(self) -> None:
        """Boolean helper only accepts the explicit truthy allowlist."""
        falsey_values = ("0", "false", "off", "no", "unexpected")

        for value in falsey_values:
            with self.subTest(value=value), mock.patch.dict(
                os.environ,
                {"FEATURE_FLAG": value},
                clear=True,
            ):
                self.assertFalse(get_bool_env("FEATURE_FLAG", default=True))

    def test_get_list_env_uses_empty_list_when_missing_without_default(self) -> None:
        """List helper returns an empty list when no env var or default exists."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_list_env("MISSING_LIST"), [])

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

    def test_debug_defaults_to_false(self) -> None:
        """Project settings default to non-debug mode."""
        self.assertFalse(settings.DEBUG)

    def test_placeholder_secret_keys_are_rejected_at_startup(self) -> None:
        """Settings reject public placeholder secret keys."""
        placeholder_values = (
            "dev-change-me",
            "replace-this-with-a-local-secret-key",
        )

        for placeholder in placeholder_values:
            command = (
                "import os;"
                f"os.environ['SECRET_KEY']='{placeholder}';"
                "os.environ.pop('DEBUG', None);"
                "import config.settings"
            )

            result = subprocess.run(
                [sys.executable, "-c", command],
                capture_output=True,
                check=False,
                text=True,
            )

            with self.subTest(placeholder=placeholder):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SECRET_KEY must be configured", result.stderr)


class HomeViewTests(SimpleTestCase):
    """Tests for the public home page view."""

    def test_home_renders(self) -> None:
        """Home page responds successfully to GET requests."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home.html")
        self.assertContains(response, "Guess Swiss municipalities on a map")
        self.assertNotContains(response, "Django project shell")

    def test_home_rejects_post_requests(self) -> None:
        """Home page only allows GET requests."""
        response = self.client.post(reverse("home"))

        self.assertEqual(response.status_code, 405)
