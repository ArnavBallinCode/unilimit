"""Django integration for UniLimit.

Requires the ``django`` extra: ``pip install "unilimit[django]"``.

```python
# settings.py
INSTALLED_APPS = [..., "unilimit.integrations.django"]  # optional: system checks
MIDDLEWARE = [
    ...,
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "unilimit.integrations.django.UniLimitMiddleware",  # after authentication
    ...,
]
UNILIMIT = {"URL": "redis://localhost:6379/0"}

# views.py
@ratelimit("login", rate="5/minute", key="ip", methods=["POST"])
def login(request): ...
```

Order of enforcement in a request (each step is one atomic batch, and a counter
already consumed in an earlier step is never consumed again):

1. ``UniLimitMiddleware``: exemptions, deny/allow lists and global rules, before URL
   resolution.
2. ``@ratelimit`` decorators, outermost first, when the view is called.
3. DRF throttles (``unilimit.integrations.drf``), after DRF authentication.

The first step that rejects ends the request; quota consumed by earlier steps stays
consumed.
"""

from __future__ import annotations

try:
    import django as _django  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'The Django integration requires Django. Install it with: pip install "unilimit[django]"'
    ) from exc

from unilimit.http import ResponseConfig
from unilimit.integrations._common import RequestState
from unilimit.integrations.django._request import request_state
from unilimit.integrations.django.conf import get_limiter
from unilimit.integrations.django.decorators import ratelimit
from unilimit.integrations.django.middleware import UniLimitMiddleware

__all__ = [
    "RequestState",
    "ResponseConfig",
    "UniLimitMiddleware",
    "get_limiter",
    "ratelimit",
    "request_state",
]
