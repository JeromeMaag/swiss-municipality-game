"""Query helpers for geodata views and services."""

from django.db.models import QuerySet

from .models import Canton, GeoDatasetVersion, Municipality


def get_current_dataset_version() -> GeoDatasetVersion | None:
    """Return the newest imported dataset version.

    Returns:
        The most recently imported dataset version, or None when no geodata has
        been imported yet.
    """
    return GeoDatasetVersion.objects.order_by("-imported_at", "-id").first()


def get_current_cantons() -> QuerySet[Canton]:
    """Return cantons for the current dataset version.

    Returns:
        A queryset of cantons ordered by abbreviation.
    """
    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        return Canton.objects.none()
    return Canton.objects.filter(dataset_version=dataset_version).order_by(
        "abbreviation"
    )


def get_current_municipalities() -> QuerySet[Municipality]:
    """Return active municipalities for the current dataset version.

    Returns:
        A queryset of active municipalities ordered by name.
    """
    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        return Municipality.objects.none()
    return (
        Municipality.objects.filter(dataset_version=dataset_version, is_active=True)
        .select_related("canton")
        .order_by("name")
    )
