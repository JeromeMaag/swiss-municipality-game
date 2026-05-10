"""URL routes for game-related views."""

from django.urls import path

from . import views


app_name = "game"

urlpatterns = [
    path("", views.index, name="index"),
    path("start/", views.start, name="start"),
    path("guess/", views.guess, name="guess"),
    path(
        "api/turn/<int:turn_id>/event/",
        views.track_turn_event,
        name="track_turn_event",
    ),
    path("summary/<int:game_id>/", views.summary, name="summary"),
]
