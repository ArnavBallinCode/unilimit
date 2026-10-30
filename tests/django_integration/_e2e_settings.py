"""Settings for the end-to-end Django project served by uvicorn and gunicorn workers."""

SECRET_KEY = "unilimit-e2e-only"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tests.django_integration._e2e_urls"
INSTALLED_APPS = ["rest_framework", "unilimit.integrations.django"]
MIDDLEWARE = ["unilimit.integrations.django.UniLimitMiddleware"]
DATABASES: dict[str, object] = {}
REST_FRAMEWORK: dict[str, object] = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_RATES": {"e2e": "100/day"},
}
UNILIMIT = {"LIMITER": "tests.django_integration._e2e_urls.limiter"}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {"django.request": {"level": "ERROR"}},  # no log line per 404/429
}
