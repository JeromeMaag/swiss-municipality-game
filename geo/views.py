"""Views for geodata pages and endpoints."""

from dataclasses import dataclass
import hashlib

from django.core.cache import cache
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.db.models import QuerySet
from django.utils.cache import patch_cache_control
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from .constants import MUNICIPALITY_LABEL_ACCESS_SESSION_KEY
from .models import GeoDatasetVersion, Village
from .selectors import (
    get_canton_for_dataset_by_abbreviation,
    get_cantons_for_dataset,
    get_current_dataset_version,
    get_municipalities_for_canton,
    get_municipalities_for_dataset,
    get_municipality_labels_for_canton,
    get_municipality_labels_for_dataset,
    get_villages_for_canton,
    get_villages_for_dataset,
)
from .serializers import (
    serialize_canton_boundaries,
    serialize_municipality_boundaries,
    serialize_municipality_labels,
    serialize_village_boundaries,
)


GEOJSON_CONTENT_TYPE = "application/geo+json"
GEOJSON_CACHE_SECONDS = 60 * 60
GEOJSON_PUBLIC_CACHE_SECONDS = 5 * 60
EMPTY_FEATURE_COLLECTION = '{"type":"FeatureCollection","features":[]}'


@dataclass(frozen=True)
class VillageBoundaryScope:
    """Resolved village boundary response scope and cache metadata."""

    villages: QuerySet[Village]
    canton_key: str
    version_key: str


def geojson_response(data: str, etag: str = "") -> HttpResponse:
    """Return a publicly cacheable GeoJSON response.

    Args:
        data: Serialized GeoJSON response data.
        etag: Optional response ETag.

    Returns:
        A response with the GeoJSON content type.
    """
    response = HttpResponse(data, content_type=GEOJSON_CONTENT_TYPE)
    if etag:
        response["ETag"] = etag
    patch_cache_control(
        response,
        public=True,
        max_age=GEOJSON_PUBLIC_CACHE_SECONDS,
        stale_while_revalidate=GEOJSON_CACHE_SECONDS,
    )
    return response


def no_store_geojson_response(data: str) -> HttpResponse:
    """Return a GeoJSON response that browsers must not cache.

    Args:
        data: Serialized GeoJSON response data.

    Returns:
        A response with no-store browser cache metadata.
    """
    response = HttpResponse(data, content_type=GEOJSON_CONTENT_TYPE)
    patch_cache_control(response, no_store=True, max_age=0)
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


def cached_geojson_response(
    request,
    name: str,
    data_builder,
    scope_builder=None,
    scope_key_builder=None,
) -> HttpResponse:
    """Return cached GeoJSON for the current dataset.

    Args:
        request: The incoming HTTP request.
        name: Boundary response name.
        data_builder: Callable receiving the current dataset version and optional
            resolved scope and returning serialized GeoJSON.
        scope_builder: Optional callable receiving the current dataset version
            and returning a resolved response scope.
        scope_key_builder: Optional callable receiving the resolved scope and
            returning a cache-key suffix for filtered responses.

    Returns:
        A GeoJSON response, or 304 when the client's cached copy is current.
    """
    dataset_version = get_current_dataset_version()
    scope = None
    if dataset_version is not None and scope_builder is not None:
        scope = scope_builder(dataset_version)
        scope_key = scope_key_builder(scope) if scope_key_builder is not None else ""
        if scope_key:
            name = f"{name}:{scope_key}"
    cache_key = cache_key_for_boundaries(name, dataset_version)
    etag = etag_for_cache_key(cache_key)
    if request_etag_matches(request, etag):
        response = HttpResponseNotModified(headers={"ETag": etag})
        patch_cache_control(
            response,
            public=True,
            max_age=GEOJSON_PUBLIC_CACHE_SECONDS,
            stale_while_revalidate=GEOJSON_CACHE_SECONDS,
        )
        return response

    data = cache.get(cache_key)
    if data is None:
        data = (
            EMPTY_FEATURE_COLLECTION
            if dataset_version is None
            else data_builder(dataset_version, scope)
        )
        cache.set(cache_key, data, GEOJSON_CACHE_SECONDS)
    return geojson_response(data, etag=etag)


def server_cached_geojson_response(name: str, data_builder) -> HttpResponse:
    """Return server-cached GeoJSON without browser caching.

    Args:
        name: Response name for the server cache key.
        data_builder: Callable receiving the current dataset version and returning
            serialized GeoJSON.

    Returns:
        A no-store GeoJSON response.
    """
    dataset_version = get_current_dataset_version()
    cache_key = cache_key_for_boundaries(name, dataset_version)
    data = cache.get(cache_key)
    if data is None:
        data = (
            EMPTY_FEATURE_COLLECTION
            if dataset_version is None
            else data_builder(dataset_version)
        )
        cache.set(cache_key, data, GEOJSON_CACHE_SECONDS)
    return no_store_geojson_response(data)


def requested_canton_filter(request, dataset_version: GeoDatasetVersion):
    """Return an optional canton filter from request query parameters."""
    abbreviation = request.GET.get("canton", "").strip().upper()
    if not abbreviation:
        return None
    canton = get_canton_for_dataset_by_abbreviation(dataset_version, abbreviation)
    if canton is None:
        raise Http404(_("Canton not found."))
    return canton


def canton_scope_key(canton) -> str:
    """Return the cache-key suffix for an optional canton filter."""
    return f"canton:{canton.abbreviation}" if canton is not None else ""


def village_boundary_scope(request, dataset_version: GeoDatasetVersion) -> VillageBoundaryScope:
    """Return village queryset and cache metadata for one request scope."""
    canton = requested_canton_filter(request, dataset_version)
    villages = (
        get_villages_for_canton(canton)
        if canton
        else get_villages_for_dataset(dataset_version)
    )
    updated_at = dataset_version.villages_updated_at
    version_key = updated_at.isoformat() if updated_at is not None else "empty"
    return VillageBoundaryScope(
        villages=villages,
        canton_key=canton_scope_key(canton),
        version_key=version_key,
    )


def village_boundary_scope_key(scope: VillageBoundaryScope) -> str:
    """Return a cache-key suffix that changes when village data changes."""
    parts = ["villages", f"updated:{scope.version_key}"]
    if scope.canton_key:
        parts.append(scope.canton_key)
    return ":".join(parts)


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
        lambda dataset_version, canton: serialize_canton_boundaries(
            [canton] if canton else get_cantons_for_dataset(dataset_version)
        ),
        lambda dataset_version: requested_canton_filter(request, dataset_version),
        canton_scope_key,
    )


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
        lambda dataset_version, canton: serialize_municipality_boundaries(
            get_municipalities_for_canton(canton)
            if canton
            else get_municipalities_for_dataset(dataset_version)
        ),
        lambda dataset_version: requested_canton_filter(request, dataset_version),
        canton_scope_key,
    )


@require_GET
def village_boundaries(request):
    """Return current village boundaries without village names.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response.
    """
    return cached_geojson_response(
        request,
        "villages",
        lambda dataset_version, scope: serialize_village_boundaries(
            scope.villages
        ),
        lambda dataset_version: village_boundary_scope(request, dataset_version),
        village_boundary_scope_key,
    )


@require_GET
def municipality_labels(request):
    """Return current municipality label points for reveal mode.

    Args:
        request: The incoming HTTP request.

    Returns:
        A GeoJSON FeatureCollection response with municipality names.
    """
    turn = require_municipality_label_access(request)
    game = turn.game
    return server_cached_geojson_response(
        municipality_label_cache_name(game),
        lambda dataset_version: serialize_municipality_labels(
            get_municipality_labels_for_canton(game.canton)
            if game.mode == game.Mode.CANTON and game.canton_id
            else get_municipality_labels_for_dataset(dataset_version)
        ),
    )


def municipality_label_cache_name(game) -> str:
    """Return a dataset-stable municipality label cache name for a game scope."""
    if game.mode == game.Mode.CANTON and game.canton_id:
        return (
            "municipality-labels:canton:"
            f"{game.canton.dataset_version_id}:{game.canton_id}"
        )
    return "municipality-labels:switzerland"


def require_municipality_label_access(request):
    """Require a revealed turn grant for the current player identity.

    Args:
        request: The incoming HTTP request.

    Returns:
        The revealed turn that grants label access.

    Raises:
        Http404: If the request is not tied to the owning user or guest's
            revealed turn.
    """
    raw_turn_id = request.GET.get("turn", "")
    try:
        turn_id = int(raw_turn_id)
    except (TypeError, ValueError) as error:
        raise Http404(_("Municipality labels not found.")) from error

    if turn_id < 1:
        raise Http404(_("Municipality labels not found."))
    if request.session.get(MUNICIPALITY_LABEL_ACCESS_SESSION_KEY) != turn_id:
        raise Http404(_("Municipality labels not found."))

    from game.identity import get_player_identity
    from game.models import Turn

    player = get_player_identity(request)
    if not player.can_own_games:
        raise Http404(_("Municipality labels not found."))

    turn = (
        Turn.objects.select_related("game__canton")
        .filter(
            player.owner_query("game"),
            pk=turn_id,
            revealed_at__isnull=False,
        )
        .first()
    )
    if turn is None:
        raise Http404(_("Municipality labels not found."))
    return turn
