"""URL routes for geodata-related views."""

from django.urls import path

from . import views


app_name = "geo"

urlpatterns = [
    path("", views.index, name="index"),
]
