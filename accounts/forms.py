"""Forms for account registration and authentication."""

from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm


class RegistrationForm(UserCreationForm):
    """Registration form for username and password signups."""

    class Meta(UserCreationForm.Meta):
        """Form metadata for user registration."""

        model = get_user_model()
        fields = ("username",)
