"""Tests for the accounts app."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse


class AuthFlowTests(TestCase):
    """Tests for registration, login, logout, and protected routes."""

    def test_register_page_renders(self) -> None:
        """Registration page renders the user creation form."""
        response = self.client.get(reverse("accounts:register"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/register.html")
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

    def test_invalid_registration_does_not_create_user(self) -> None:
        """Invalid registration redisplays the form without creating a user."""
        response = self.client.post(
            reverse("accounts:register"),
            {
                "username": "newplayer",
                "password1": "StrongPass123!",
                "password2": "DifferentPass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/register.html")
        self.assertFalse(get_user_model().objects.filter(username="newplayer").exists())
        self.assertNotIn("_auth_user_id", self.client.session)

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
        self.assertTemplateUsed(response, "accounts/login.html")
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

    def test_invalid_login_does_not_authenticate(self) -> None:
        """Invalid login redisplays the form without authenticating the user."""
        get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )

        response = self.client.post(
            reverse("accounts:login"),
            {"username": "player", "password": "WrongPass123!"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "accounts/login.html")
        self.assertNotIn("_auth_user_id", self.client.session)

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

    def test_login_rejects_external_next_url(self) -> None:
        """Login does not redirect to unsafe external next URLs."""
        get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )

        response = self.client.post(
            f"{reverse('accounts:login')}?next=https://example.test/phishing",
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


class AuthCsrfTests(TestCase):
    """Tests that auth-changing requests require CSRF protection."""

    def setUp(self) -> None:
        """Create a CSRF-enforcing test client."""
        self.csrf_client = Client(enforce_csrf_checks=True)

    def test_register_requires_csrf_token(self) -> None:
        """Registration POSTs without CSRF tokens are rejected."""
        response = self.csrf_client.post(
            reverse("accounts:register"),
            {
                "username": "newplayer",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_login_requires_csrf_token(self) -> None:
        """Login POSTs without CSRF tokens are rejected."""
        response = self.csrf_client.post(
            reverse("accounts:login"),
            {"username": "player", "password": "StrongPass123!"},
        )

        self.assertEqual(response.status_code, 403)

    def test_logout_requires_csrf_token(self) -> None:
        """Logout POSTs without CSRF tokens are rejected."""
        user = get_user_model().objects.create_user(
            username="player",
            password="StrongPass123!",
        )
        self.csrf_client.force_login(user)

        response = self.csrf_client.post(reverse("accounts:logout"))

        self.assertEqual(response.status_code, 403)
