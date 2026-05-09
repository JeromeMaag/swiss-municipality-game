"""Views for account-related pages."""

from django.contrib.auth import login
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.contrib.auth.views import LogoutView as DjangoLogoutView
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic.edit import FormView

from .forms import RegistrationForm


class LoginView(DjangoLoginView):
    """Render and process the login form."""

    template_name = "accounts/login.html"
    redirect_authenticated_user = True


class LogoutView(DjangoLogoutView):
    """Log out authenticated users via POST."""

    http_method_names = ["post", "options"]


class RegisterView(FormView):
    """Render and process the registration form."""

    form_class = RegistrationForm
    template_name = "accounts/register.html"
    success_url = reverse_lazy("game:index")

    def dispatch(self, request, *args, **kwargs):
        """Redirect authenticated users away from registration.

        Args:
            request: The incoming HTTP request.
            *args: Positional view arguments.
            **kwargs: Keyword view arguments.

        Returns:
            An HTTP response for the registration flow or redirect.
        """
        if request.user.is_authenticated:
            return redirect(self.success_url)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        """Create the user and authenticate the new session.

        Args:
            form: The validated registration form.

        Returns:
            A redirect response to the configured success URL.
        """
        user = form.save()
        login(
            self.request,
            user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        return super().form_valid(form)
