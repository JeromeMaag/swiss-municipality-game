"""Query helpers for geodata views and services."""

from django.db.models import QuerySet

from .constants import DEV_GEODATA_DATASET_NAME
from .models import Canton, GeoDatasetVersion, Municipality


DEVELOPMENT_DATASET_NAMES = frozenset({DEV_GEODATA_DATASET_NAME})


def get_current_dataset_version() -> GeoDatasetVersion | None:
    """Return the newest non-development dataset version.

    Returns:
        The most recently imported non-development dataset version, falling back
        to development seed data only when no regular geodata exists.
    """
    ordering = ("-imported_at", "-id")
    dataset_version = (
        GeoDatasetVersion.objects.exclude(name__in=DEVELOPMENT_DATASET_NAMES)
        .order_by(*ordering)
        .first()
    )
    if dataset_version is not None:
        return dataset_version
    return GeoDatasetVersion.objects.order_by(*ordering).first()


def get_current_cantons() -> QuerySet[Canton]:
    """Return cantons for the current dataset version.

    Returns:
        A queryset of cantons ordered by abbreviation.
    """
    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        return Canton.objects.none()
    return get_cantons_for_dataset(dataset_version)


def get_cantons_for_dataset(dataset_version: GeoDatasetVersion) -> QuerySet[Canton]:
    """Return cantons for one dataset version.

    Args:
        dataset_version: Dataset version to query.

    Returns:
        A queryset of cantons ordered by abbreviation.
    """
    return Canton.objects.filter(dataset_version=dataset_version).order_by(
        "abbreviation"
    )


def get_canton_for_dataset_by_abbreviation(
    dataset_version: GeoDatasetVersion,
    abbreviation: str,
) -> Canton | None:
    """Return one canton from a dataset by abbreviation.

    Args:
        dataset_version: Dataset version to query.
        abbreviation: Canton abbreviation such as ``ZH``.

    Returns:
        The matching canton, or None.
    """
    return (
        get_cantons_for_dataset(dataset_version)
        .filter(abbreviation=abbreviation.strip().upper())
        .first()
    )


def get_current_municipalities() -> QuerySet[Municipality]:
    """Return active municipalities for the current dataset version.

    Returns:
        A queryset of active municipalities ordered by internal id.
    """
    dataset_version = get_current_dataset_version()
    if dataset_version is None:
        return Municipality.objects.none()
    return get_municipalities_for_dataset(dataset_version)


def get_municipalities_for_dataset(
    dataset_version: GeoDatasetVersion,
) -> QuerySet[Municipality]:
    """Return active municipalities for one dataset version.

    Args:
        dataset_version: Dataset version to query.

    Returns:
        A queryset of active municipalities ordered by internal id.
    """
    return Municipality.objects.filter(
        dataset_version=dataset_version,
        is_active=True,
    ).order_by("id")


def get_municipalities_for_canton(canton: Canton) -> QuerySet[Municipality]:
    """Return active municipalities for one canton.

    Args:
        canton: Canton to query.

    Returns:
        A queryset of active municipalities ordered by internal id.
    """
    return Municipality.objects.filter(canton=canton, is_active=True).order_by("id")


def get_municipality_labels_for_dataset(
    dataset_version: GeoDatasetVersion,
) -> QuerySet[Municipality]:
    """Return active municipalities with label points for one dataset version.

    Args:
        dataset_version: Dataset version to query.

    Returns:
        A queryset of active municipalities prepared for label serialization.
    """
    return (
        Municipality.objects.filter(
            dataset_version=dataset_version,
            is_active=True,
            label_point__isnull=False,
        )
        .only(
            "id",
            "name",
            "label_point",
        )
        .order_by("id")
    )


def get_municipality_labels_for_canton(canton: Canton) -> QuerySet[Municipality]:
    """Return active municipality labels for one canton.

    Args:
        canton: Canton to query.

    Returns:
        A queryset of active municipalities prepared for label serialization.
    """
    return (
        Municipality.objects.filter(
            canton=canton,
            is_active=True,
            label_point__isnull=False,
        )
        .only(
            "id",
            "name",
            "label_point",
        )
        .order_by("id")
    )
