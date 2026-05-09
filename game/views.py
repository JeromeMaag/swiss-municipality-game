"""Views for game pages."""

from django.http import HttpResponse
from django.views.decorators.http import require_GET


@require_GET
def index(request):
    """Render the temporary game entry page.

    Args:
        request: The incoming HTTP request.

    Returns:
        A plain HTTP response until the real game page is implemented.
    """
    return HttpResponse("Game setup will be implemented in a later step.")
