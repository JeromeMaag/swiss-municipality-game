"""Views for geodata pages and endpoints."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.views.decorators.http import require_GET

from .selectors import get_current_cantons, get_current_municipalities
from .serializers import (
    serialize_canton_boundaries,
    serialize_municipality_boundaries,
)


GEOJSON_CONTENT_TYPE = "application/geo+json"


@require_GET
def index(request):
    """Render a temporary geodata page placeholder.

    Args:
        request: The incoming HTTP request.

    Returns:
        A plain HTTP response while the geodata page UI is pending.
    """
    return HttpResponse("Geodata API endpoints are available; page UI is pending.")


def geojson_response(data: str) -> HttpResponse:
    """Return a GeoJSON response.

    Args:
        data: Serialized GeoJSON response data.

    Returns:
        A response with the GeoJSON content type.
    """
    return HttpResponse(data, content_type=GEOJSON_CONTENT_TYPE)


@login_required
@require_GET
def canton_boundaries(request):
    """Return current canton boundaries as GeoJSON.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response.
    """
    return geojson_response(serialize_canton_boundaries(get_current_cantons()))


@login_required
@require_GET
def municipality_boundaries(request):
    """Return current municipality boundaries without municipality names.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response.
    """
    return geojson_response(
        serialize_municipality_boundaries(get_current_municipalities())
    )
