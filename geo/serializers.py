"""GeoJSON serializers for geodata models."""

import json
from collections.abc import Callable, Iterable
from typing import Any

from django.contrib.gis.geos import GEOSGeometry

from .models import Canton, Municipality


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


def get_display_geometry(obj: Canton | Municipality) -> GEOSGeometry:
    """Return the simplified geometry when available, otherwise the full geometry.

    Args:
        obj: Canton or municipality instance.

    Returns:
        The geometry intended for map display.
    """
    return obj.geom_simplified or obj.geom


def serialize_canton_boundaries(cantons: Iterable[Canton]) -> str:
    """Serialize canton boundary features.

    Args:
        cantons: Canton objects to serialize.

    Returns:
        A GeoJSON FeatureCollection string with canton properties.
    """
    return feature_collection(
        cantons,
        get_display_geometry,
        lambda canton: {
            "id": canton.id,
            "bfs_number": canton.bfs_number,
            "abbreviation": canton.abbreviation,
            "name": canton.name,
        },
    )


def serialize_municipality_boundaries(
    municipalities: Iterable[Municipality],
) -> str:
    """Serialize municipality boundaries without gameplay-spoiling names.

    Args:
        municipalities: Municipality objects to serialize.

    Returns:
        A GeoJSON FeatureCollection string with neutral municipality identifiers.
    """
    return feature_collection(
        municipalities,
        get_display_geometry,
        lambda municipality: {
            "id": municipality.id,
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
            "canton_abbreviation": municipality.canton.abbreviation,
        },
    )
