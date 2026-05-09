"""Project-level views."""

from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def home(request):
    """Render the public home page.

    Args:
        request: The incoming HTTP request.

    Returns:
        An HTTP response containing the home page.
    """
    return render(request, "home.html")
