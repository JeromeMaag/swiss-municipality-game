"""URL routes for account-related views."""

from django.urls import path

from . import views


app_name = "accounts"

urlpatterns = [
    path("login/", views.login_placeholder, name="login"),
    path("register/", views.register_placeholder, name="register"),
]
