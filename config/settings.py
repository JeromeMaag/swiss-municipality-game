"""Django settings for the GemeindeGuess CH project."""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def get_bool_env(name: str, default: bool = False) -> bool:
    """Read a boolean value from an environment variable.

    Args:
        name: Name of the environment variable.
        default: Fallback value when the variable is not set.

    Returns:
        The parsed boolean value.
    """
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def get_list_env(name: str, default: list[str] | None = None) -> list[str]:
    """Read a comma-separated list from an environment variable.

    Args:
        name: Name of the environment variable.
        default: Fallback list when the variable is not set.

    Returns:
        A list of non-empty trimmed values.
    """
    value = os.getenv(name)
    if value is None:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


def get_geodjango_library_path(
    env_name: str,
    package_glob: str,
    *,
    base_dir: Path = BASE_DIR,
    os_name: str | None = None,
) -> str | None:
    """Return a configured or auto-detected GeoDjango native library path.

    Args:
        env_name: Environment variable that can explicitly define the path.
        package_glob: Glob below ``.venv/Lib/site-packages`` to auto-detect.
        base_dir: Project root used for auto-detection.
        os_name: Operating system name, defaulting to ``os.name``.

    Returns:
        A configured or detected library path, or None when unavailable.
    """
    configured_path = os.getenv(env_name)
    if configured_path:
        return configured_path

    if (os_name or os.name) != "nt":
        return None

    site_packages_dir = base_dir / ".venv" / "Lib" / "site-packages"
    matches = sorted(site_packages_dir.glob(package_glob))
    if not matches:
        return None
    return str(matches[0])


SECRET_KEY_PLACEHOLDERS = {
    "dev-change-me",
    "replace-this-with-a-local-secret-key",
}
SECRET_KEY = os.getenv("SECRET_KEY", "")

DEBUG = get_bool_env("DEBUG", default=False)

if not SECRET_KEY or SECRET_KEY in SECRET_KEY_PLACEHOLDERS:
    raise ValueError("SECRET_KEY must be configured with a non-placeholder value.")

ALLOWED_HOSTS = get_list_env("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "accounts.apps.AccountsConfig",
    "geo.apps.GeoConfig",
    "game.apps.GameConfig",
    "tracking.apps.TrackingConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default="postgis://gemeindeguess:gemeindeguess@localhost:5432/gemeindeguess",
        conn_max_age=600,
        engine="django.contrib.gis.db.backends.postgis",
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en"

LANGUAGES = [
    ("en", "English"),
    ("de", "Deutsch"),
    ("fr", "Français"),
]

LOCALE_PATHS = [BASE_DIR / "locale"]

TIME_ZONE = "Europe/Zurich"

USE_I18N = True

USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "game:index"
LOGOUT_REDIRECT_URL = "home"

GDAL_LIBRARY_PATH = get_geodjango_library_path(
    "GDAL_LIBRARY_PATH",
    "pyogrio.libs/gdal*.dll",
)
GEOS_LIBRARY_PATH = get_geodjango_library_path(
    "GEOS_LIBRARY_PATH",
    "shapely.libs/geos_c*.dll",
)
