"""Tests for project-level helpers and views."""

import os
import subprocess
import sys
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.translation import deactivate

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
        """Project settings default to non-debug mode when DEBUG is unset."""
        command = (
            "import os, dotenv;"
            "os.environ['SECRET_KEY']='valid-test-secret-key';"
            "os.environ.pop('DEBUG', None);"
            "dotenv.load_dotenv=lambda *args, **kwargs: False;"
            "import config.settings;"
            "print(config.settings.DEBUG)"
        )

        result = subprocess.run(
            [sys.executable, "-c", command],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "False")

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


class HomeViewTests(TestCase):
    """Tests for the root entry view."""

    def test_home_redirects_to_game(self) -> None:
        """Anonymous users enter the game shell from the root URL."""
        response = self.client.get(reverse("home"))

        self.assertRedirects(response, reverse("game:index"))

    def test_authenticated_home_redirects_to_game(self) -> None:
        """Authenticated users enter the game flow from the root URL."""
        user = get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        self.assertRedirects(response, reverse("game:index"))

    def test_home_rejects_post_requests(self) -> None:
        """Home page only allows GET requests."""
        response = self.client.post(reverse("home"))

        self.assertEqual(response.status_code, 405)


class LanguagePreferenceTests(TestCase):
    """Tests for browser and manual language selection."""

    def setUp(self) -> None:
        """Create a user for profile language settings."""
        self.user = get_user_model().objects.create_user(
            username="language-player",
            password="StrongPass123!",
        )

    def tearDown(self) -> None:
        """Reset the active thread language after language tests."""
        deactivate()

    def test_browser_language_sets_active_template_language(self) -> None:
        """Locale middleware uses the browser language before a manual choice."""
        response = self.client.get(
            reverse("accounts:login"),
            HTTP_ACCEPT_LANGUAGE="fr",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<html lang="fr">')
        self.assertContains(response, "Trouve la commune !")
        self.assertContains(response, "Connexion")

    def test_profile_renders_language_setting(self) -> None:
        """Profile settings expose the language selector for signed-in users."""
        self.client.force_login(self.user)

        response = self.client.get(reverse("profile"), HTTP_ACCEPT_LANGUAGE="fr")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paramètres")
        self.assertContains(response, 'action="/i18n/setlang/"')
        self.assertContains(response, 'id="profile-language-select"')
        self.assertContains(response, 'name="language"')
        self.assertContains(response, 'onchange="this.form.submit()"')
        self.assertContains(response, 'value="fr" selected')
        self.assertContains(response, "English")
        self.assertContains(response, "Deutsch")
        self.assertContains(response, "Français")
        self.assertNotContains(response, "Anglais")
        self.assertNotContains(response, "Allemand")

    def test_language_switch_stores_cookie_and_redirects_back(self) -> None:
        """The profile language selector persists a manual language choice."""
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("set_language"),
            {
                "language": "de",
                "next": reverse("accounts:login"),
            },
        )

        self.assertRedirects(
            response,
            reverse("accounts:login"),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME].value,
            "de",
        )

        followup_response = self.client.get(reverse("profile"))

        self.assertContains(followup_response, '<html lang="de">')
        self.assertContains(followup_response, "Finde die Gemeinde!")
        self.assertContains(followup_response, "Einstellungen")
        self.assertContains(followup_response, 'value="de" selected')
