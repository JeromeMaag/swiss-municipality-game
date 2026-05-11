"""Project-level views."""

from django.shortcuts import redirect
from django.views.decorators.http import require_GET


@require_GET
def home(request):
    """Route users into the game entry screen.

    Args:
        request: The incoming HTTP request.

    Returns:
        A redirect to the game entry page.
    """
    return redirect("game:index")
