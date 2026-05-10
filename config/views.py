"""Project-level views."""

from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET


@require_GET
def home(request):
    """Route users into the right entry screen.

    Args:
        request: The incoming HTTP request.

    Returns:
        A redirect to the game for authenticated users, otherwise the public
        home page.
    """
    if request.user.is_authenticated:
        return redirect("game:index")
    return render(request, "home.html")
