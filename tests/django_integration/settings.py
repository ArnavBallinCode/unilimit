"""Django settings for the integration tests (``DJANGO_SETTINGS_MODULE``)."""

import os
import tempfile
from pathlib import Path

SECRET_KEY = "unilimit-tests-only"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tests.django_integration.urls"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "unilimit.integrations.django",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "unilimit.integrations.django.UniLimitMiddleware",
]
# A file, not ":memory:": Django never closes in-memory SQLite connections, and the
# ASGI handler opens one per request thread, which the per-test leak check reports.
_DB = str(Path(tempfile.gettempdir()) / f"unilimit-django-tests-{os.getpid()}.sqlite3")
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": _DB, "TEST": {"NAME": _DB}}
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "tests.django_integration.auth.HeaderAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {"anon": "2/minute", "user": "3/minute", "search": "2/minute"},
}
UNILIMIT = {"URL": "memory://"}
LOGGING_CONFIG = None
