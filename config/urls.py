"""URL configuration for the GemeindeGuess CH project."""

from django.contrib import admin
from django.urls import include, path

from .views import home


urlpatterns = [
    path("", home, name="home"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("geo/", include("geo.urls")),
    path("game/", include("game.urls")),
]
