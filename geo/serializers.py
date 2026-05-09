"""GeoJSON serializers for geodata models."""

import json
from collections.abc import Callable, Iterable
from typing import Any

from django.contrib.gis.geos import GEOSGeometry

from .models import Canton, Municipality


FeatureProperties = dict[str, Any]
GeometryGetter = Callable[[Any], GEOSGeometry | None]
PropertiesGetter = Callable[[Any], FeatureProperties]


def geometry_to_geojson(geometry: GEOSGeometry) -> dict[str, Any]:
    """Convert a GEOS geometry to a GeoJSON geometry mapping.

    Args:
        geometry: Geometry to serialize.

    Returns:
        A GeoJSON geometry dictionary.
    """
    return json.loads(geometry.geojson)


def feature_collection(
    objects: Iterable[Any],
    geometry_getter: GeometryGetter,
    properties_getter: PropertiesGetter,
) -> dict[str, Any]:
    """Serialize objects as a GeoJSON FeatureCollection.

    Args:
        objects: Objects to serialize.
        geometry_getter: Function returning the object's geometry.
        properties_getter: Function returning the object's properties.

    Returns:
        A GeoJSON FeatureCollection dictionary.
    """
    features = []
    for obj in objects:
        geometry = geometry_getter(obj)
        if geometry is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry_to_geojson(geometry),
                "properties": properties_getter(obj),
            }
        )
    return {"type": "FeatureCollection", "features": features}


def get_display_geometry(obj: Canton | Municipality) -> GEOSGeometry:
    """Return the simplified geometry when available, otherwise the full geometry.

    Args:
        obj: Canton or municipality instance.

    Returns:
        The geometry intended for map display.
    """
    return obj.geom_simplified or obj.geom


def serialize_canton_boundaries(cantons: Iterable[Canton]) -> dict[str, Any]:
    """Serialize canton boundary features.

    Args:
        cantons: Canton objects to serialize.

    Returns:
        A GeoJSON FeatureCollection with canton properties.
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
) -> dict[str, Any]:
    """Serialize municipality boundaries without gameplay-spoiling names.

    Args:
        municipalities: Municipality objects to serialize.

    Returns:
        A GeoJSON FeatureCollection with neutral municipality identifiers.
    """
    return feature_collection(
        municipalities,
        get_display_geometry,
        lambda municipality: {
            "id": municipality.id,
        },
    )
