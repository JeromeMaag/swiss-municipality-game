"""Views for geodata pages and endpoints."""

from django.http import HttpResponse
from django.views.decorators.http import require_GET


@require_GET
def index(request):
    """Render a temporary geodata placeholder.

    Args:
        request: The incoming HTTP request.

    Returns:
        A plain HTTP response until geodata endpoints are implemented.
    """
    return HttpResponse("Geodata endpoints will be implemented in a later step.")
