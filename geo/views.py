"""Views for geodata pages and endpoints."""

import hashlib

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.utils.cache import patch_cache_control
from django.views.decorators.http import require_GET

from .constants import MUNICIPALITY_LABEL_ACCESS_SESSION_KEY
from .models import GeoDatasetVersion
from .selectors import (
    get_cantons_for_dataset,
    get_current_dataset_version,
    get_municipalities_for_dataset,
    get_municipality_labels_for_dataset,
)
from .serializers import (
    serialize_canton_boundaries,
    serialize_municipality_boundaries,
    serialize_municipality_labels,
)


GEOJSON_CONTENT_TYPE = "application/geo+json"
GEOJSON_CACHE_SECONDS = 60 * 60
EMPTY_FEATURE_COLLECTION = '{"type":"FeatureCollection","features":[]}'


@require_GET
def index(request):
    """Render a temporary geodata page placeholder.

    Args:
        request: The incoming HTTP request.

    Returns:
        A plain HTTP response while the geodata page UI is pending.
    """
    return HttpResponse("Geodata API endpoints are available; page UI is pending.")


def geojson_response(data: str, etag: str = "") -> HttpResponse:
    """Return a GeoJSON response.

    Args:
        data: Serialized GeoJSON response data.
        etag: Optional response ETag.

    Returns:
        A response with the GeoJSON content type.
    """
    response = HttpResponse(data, content_type=GEOJSON_CONTENT_TYPE)
    if etag:
        response["ETag"] = etag
    patch_cache_control(response, private=True, max_age=GEOJSON_CACHE_SECONDS)
    return response


def cache_key_for_boundaries(name: str, dataset_version: GeoDatasetVersion | None) -> str:
    """Build a cache key for the current boundary dataset.

    Args:
        name: Boundary response name.
        dataset_version: Dataset version used for the response.

    Returns:
        A stable cache key that changes when the current dataset changes.
    """
    if dataset_version is None:
        return f"geojson:{name}:empty"
    return (
        f"geojson:{name}:{dataset_version.id}:"
        f"{dataset_version.imported_at.isoformat()}"
    )


def etag_for_cache_key(cache_key: str) -> str:
    """Build a strong ETag from a boundary cache key.

    Args:
        cache_key: Boundary cache key.

    Returns:
        Quoted ETag header value.
    """
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return f'"{digest}"'


def request_etag_matches(request, etag: str) -> bool:
    """Return whether a request already has the current boundary ETag.

    Args:
        request: The incoming HTTP request.
        etag: Current boundary ETag.

    Returns:
        True when the request's If-None-Match header matches the ETag.
    """
    header_value = request.headers.get("If-None-Match", "")
    client_etags = [value.strip() for value in header_value.split(",")]
    return "*" in client_etags or etag in client_etags


def cached_geojson_response(request, name: str, data_builder) -> HttpResponse:
    """Return cached GeoJSON for the current dataset.

    Args:
        request: The incoming HTTP request.
        name: Boundary response name.
        data_builder: Callable receiving the current dataset version and returning
            serialized GeoJSON.

    Returns:
        A GeoJSON response, or 304 when the client's cached copy is current.
    """
    dataset_version = get_current_dataset_version()
    cache_key = cache_key_for_boundaries(name, dataset_version)
    etag = etag_for_cache_key(cache_key)
    if request_etag_matches(request, etag):
        response = HttpResponseNotModified(headers={"ETag": etag})
        patch_cache_control(response, private=True, max_age=GEOJSON_CACHE_SECONDS)
        return response

    data = cache.get(cache_key)
    if data is None:
        data = (
            EMPTY_FEATURE_COLLECTION
            if dataset_version is None
            else data_builder(dataset_version)
        )
        cache.set(cache_key, data, GEOJSON_CACHE_SECONDS)
    return geojson_response(data, etag=etag)


@login_required
@require_GET
def canton_boundaries(request):
    """Return current canton boundaries as GeoJSON.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response.
    """
    return cached_geojson_response(
        request,
        "cantons",
        lambda dataset_version: serialize_canton_boundaries(
            get_cantons_for_dataset(dataset_version)
        ),
    )


@login_required
@require_GET
def municipality_boundaries(request):
    """Return current municipality boundaries without municipality names.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response.
    """
    return cached_geojson_response(
        request,
        "municipalities",
        lambda dataset_version: serialize_municipality_boundaries(
            get_municipalities_for_dataset(dataset_version)
        ),
    )


@login_required
@require_GET
def municipality_labels(request):
    """Return current municipality label points for reveal mode.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response with municipality names.
    """
    require_municipality_label_access(request)
    return cached_geojson_response(
        request,
        "municipality-labels",
        lambda dataset_version: serialize_municipality_labels(
            get_municipality_labels_for_dataset(dataset_version)
        ),
    )


def require_municipality_label_access(request) -> None:
    """Require a revealed turn grant before returning municipality labels.

    Args:
        request: The incoming HTTP request.

    Raises:
        Http404: If the request is not tied to the current user's revealed turn.
    """
    raw_turn_id = request.GET.get("turn", "")
    try:
        turn_id = int(raw_turn_id)
    except (TypeError, ValueError) as error:
        raise Http404("Municipality labels not found.") from error

    if turn_id < 1:
        raise Http404("Municipality labels not found.")
    if request.session.get(MUNICIPALITY_LABEL_ACCESS_SESSION_KEY) != turn_id:
        raise Http404("Municipality labels not found.")

    from game.models import Turn

    if not Turn.objects.filter(
        pk=turn_id,
        game__user=request.user,
        revealed_at__isnull=False,
    ).exists():
        raise Http404("Municipality labels not found.")
