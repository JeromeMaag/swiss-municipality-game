"""Shared utilities for tests."""

from django.contrib.gis.geos import MultiPolygon, Polygon


def make_test_geometry() -> MultiPolygon:
    """Create a simple WGS84 multipolygon for model tests.

    Returns:
        A square multipolygon with SRID 4326.
    """
    polygon = Polygon(
        ((8.0, 47.0), (8.1, 47.0), (8.1, 47.1), (8.0, 47.1), (8.0, 47.0))
    )
    return MultiPolygon(polygon, srid=4326)
