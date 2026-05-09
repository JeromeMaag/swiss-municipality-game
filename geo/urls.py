"""URL routes for geodata-related views."""

from django.urls import path

from . import views


app_name = "geo"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/cantons.geojson/", views.canton_boundaries, name="cantons_geojson"),
    path(
        "api/municipality-boundaries.geojson/",
        views.municipality_boundaries,
        name="municipality_boundaries_geojson",
    ),
]
