"""Tests for the accounts app."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AuthFlowTests(TestCase):
    """Tests for registration, login, logout, and protected routes."""

    def test_register_page_renders(self) -> None:
        """Registration page renders the user creation form."""
        response = self.client.get(reverse("accounts:register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Register")
        self.assertContains(response, "name=\"username\"")

    def test_register_creates_user_and_logs_in(self) -> None:
        """Registration creates a user and authenticates the new session."""
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "newplayer",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertRedirects(response, reverse("game:index"))
        self.assertTrue(get_user_model().objects.filter(username="newplayer").exists())
        user = get_user_model().objects.get(username="newplayer")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.id)

    def test_authenticated_user_is_redirected_from_register(self) -> None:
        """Authenticated users do not see the registration form again."""
        user = get_user_model().objects.create_user(
            username="existing",
            password="StrongPass123!",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("accounts:register"))

        self.assertRedirects(response, reverse("game:index"))

    def test_login_page_renders(self) -> None:
        """Login page renders the authentication form."""
        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Login")
        self.assertContains(response, "name=\"username\"")

    def test_login_authenticates_existing_user(self) -> None:
        """Login authenticates an existing user."""
        user = get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )

        response = self.client.post(
            reverse("accounts:login"),
            {"username": "player", "password": "StrongPass123!"},
        )

        self.assertRedirects(response, reverse("game:index"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.id)

    def test_login_redirects_to_next_url(self) -> None:
        """Login redirects to the requested next URL when provided."""
        get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )

        response = self.client.post(
            f"{reverse('accounts:login')}?next={reverse('game:index')}",
            {"username": "player", "password": "StrongPass123!"},
        )

        self.assertRedirects(response, reverse("game:index"))

    def test_logout_requires_post_and_ends_session(self) -> None:
        """Logout accepts POST and removes the authenticated session."""
        user = get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )
        self.client.force_login(user)

        get_response = self.client.get(reverse("accounts:logout"))
        post_response = self.client.post(reverse("accounts:logout"))

        self.assertEqual(get_response.status_code, 405)
        self.assertRedirects(post_response, reverse("home"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_game_index_requires_login(self) -> None:
        """Game index redirects anonymous users to login."""
        response = self.client.get(reverse("game:index"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('game:index')}",
        )
