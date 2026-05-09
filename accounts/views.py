"""Views for account-related pages."""

from django.http import HttpResponse
from django.views.decorators.http import require_GET


@require_GET
def login_placeholder(request):
    """Render a temporary login placeholder.

    Args:
        request: The incoming HTTP request.

    Returns:
        A plain HTTP response until the real login view is implemented.
    """
    return HttpResponse("Login will be implemented in a later step.")


@require_GET
def register_placeholder(request):
    """Render a temporary registration placeholder.

    Args:
        request: The incoming HTTP request.

    Returns:
        A plain HTTP response until the real registration view is implemented.
    """
    return HttpResponse("Registration will be implemented in a later step.")
