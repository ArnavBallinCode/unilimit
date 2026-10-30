"""ASGI and WSGI entry points of the end-to-end Django project."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.django_integration._e2e_settings")

from django.core.asgi import get_asgi_application
from django.core.wsgi import get_wsgi_application

from unilimit.integrations.django.asgi import with_lifespan

asgi_application = with_lifespan(get_asgi_application())
wsgi_application = get_wsgi_application()
