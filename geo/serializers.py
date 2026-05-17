"""GeoJSON serializers for geodata models."""

import json
from collections.abc import Callable, Iterable
from typing import Any

from django.contrib.gis.geos import GEOSGeometry

from .models import Canton, Municipality, Village


BOUNDARY_DETAIL_FULL = "full"
BOUNDARY_DETAIL_SIMPLE = "simple"
BOUNDARY_DETAILS = {BOUNDARY_DETAIL_FULL, BOUNDARY_DETAIL_SIMPLE}

FeatureProperties = dict[str, Any]
GeometryGetter = Callable[[Any], GEOSGeometry | None]
PropertiesGetter = Callable[[Any], FeatureProperties]


def geometry_to_geojson(geometry: GEOSGeometry) -> str:
    """Convert a GEOS geometry to a GeoJSON geometry string.

    Args:
        geometry: Geometry to serialize.

    Returns:
        A GeoJSON geometry string.
    """
    return geometry.geojson


def feature_collection(
    objects: Iterable[Any],
    geometry_getter: GeometryGetter,
    properties_getter: PropertiesGetter,
) -> str:
    """Serialize objects as a GeoJSON FeatureCollection string.

    Args:
        objects: Objects to serialize.
        geometry_getter: Function returning the object's geometry.
        properties_getter: Function returning the object's properties.

    Returns:
        A GeoJSON FeatureCollection string.
    """
    features = []
    for obj in objects:
        geometry = geometry_getter(obj)
        if geometry is None:
            continue
        properties = json.dumps(
            properties_getter(obj),
            allow_nan=False,
            separators=(",", ":"),
        )
        features.append(
            '{"type":"Feature","geometry":'
            f"{geometry_to_geojson(geometry)},"
            f'"properties":{properties}'
            "}"
        )
    return '{"type":"FeatureCollection","features":[' + ",".join(features) + "]}"


def get_boundary_geometry(
    obj: Canton | Municipality | Village,
    detail: str = BOUNDARY_DETAIL_SIMPLE,
) -> GEOSGeometry:
    """Return a boundary geometry for the requested map detail.

    Args:
        obj: Canton, municipality, or village instance.
        detail: Boundary detail level. ``simple`` prefers simplified geometry;
            ``full`` returns the original geometry.

    Returns:
        The geometry intended for map display at this detail level.
    """
    if detail == BOUNDARY_DETAIL_FULL:
        return obj.geom
    return obj.geom_simplified or obj.geom


def get_display_geometry(obj: Canton | Municipality | Village) -> GEOSGeometry:
    """Return the default display geometry for a boundary object."""
    return get_boundary_geometry(obj, BOUNDARY_DETAIL_SIMPLE)


def serialize_canton_boundaries(
    cantons: Iterable[Canton],
    detail: str = BOUNDARY_DETAIL_SIMPLE,
) -> str:
    """Serialize canton boundary features.

    Args:
        cantons: Canton objects to serialize.
        detail: Boundary detail level.

    Returns:
        A GeoJSON FeatureCollection string with canton properties.
    """
    return feature_collection(
        cantons,
        lambda canton: get_boundary_geometry(canton, detail),
        lambda canton: {
            "id": canton.id,
            "bfs_number": canton.bfs_number,
            "abbreviation": canton.abbreviation,
            "name": canton.name,
        },
    )


def serialize_municipality_boundaries(
    municipalities: Iterable[Municipality],
    detail: str = BOUNDARY_DETAIL_SIMPLE,
) -> str:
    """Serialize municipality boundaries without gameplay-spoiling names.

    Args:
        municipalities: Municipality objects to serialize.
        detail: Boundary detail level.

    Returns:
        A GeoJSON FeatureCollection string with neutral municipality identifiers.
    """
    return feature_collection(
        municipalities,
        lambda municipality: get_boundary_geometry(municipality, detail),
        lambda municipality: {
            "id": municipality.id,
        },
    )


def serialize_village_boundaries(
    villages: Iterable[Village],
    detail: str = BOUNDARY_DETAIL_SIMPLE,
) -> str:
    """Serialize village boundaries without gameplay-spoiling names.

    Args:
        villages: Village objects to serialize.
        detail: Boundary detail level.

    Returns:
        A GeoJSON FeatureCollection string with neutral village identifiers.
    """
    return feature_collection(
        villages,
        lambda village: get_boundary_geometry(village, detail),
        lambda village: {
            "id": village.id,
        },
    )


def serialize_municipality_labels(municipalities: Iterable[Municipality]) -> str:
    """Serialize municipality label points with names for reveal mode.

    Args:
        municipalities: Municipality objects to serialize.

    Returns:
        A GeoJSON FeatureCollection string with label point features.
    """
    return feature_collection(
        municipalities,
        lambda municipality: municipality.label_point,
        lambda municipality: {
            "id": municipality.id,
            "name": municipality.name,
        },
    )
