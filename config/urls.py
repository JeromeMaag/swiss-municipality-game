"""URL configuration for the Find the Municipality! project."""

from django.contrib import admin
from django.urls import include, path

from accounts.views import profile, profile_stats
from geo.admin_views import geodata_setup

from .views import home


admin.site.index_template = "admin/geodata_index.html"


urlpatterns = [
    path("", home, name="home"),
    path("profile/", profile, name="profile"),
    path("profile/stats/", profile_stats, name="profile_stats"),
    path(
        "admin/geodata/setup/",
        admin.site.admin_view(geodata_setup),
        name="admin_geodata_setup",
    ),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("geo/", include("geo.urls")),
    path("game/", include("game.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
]
