"""URL routes for game-related views."""

from django.urls import path

from . import views


app_name = "game"

urlpatterns = [
    path("", views.index, name="index"),
    path("start/", views.start, name="start"),
]
