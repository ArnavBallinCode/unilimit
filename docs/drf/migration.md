# Migrating from DRF's built-in throttles

Swap the classes; keep your settings.

| DRF | UniLimit |
|---|---|
| `rest_framework.throttling.AnonRateThrottle` | `unilimit.integrations.drf.UniLimitAnonRateThrottle` |
| `rest_framework.throttling.UserRateThrottle` | `unilimit.integrations.drf.UniLimitUserRateThrottle` |
| `rest_framework.throttling.ScopedRateThrottle` | `unilimit.integrations.drf.UniLimitScopedRateThrottle` |
| `SimpleRateThrottle` subclass | `UniLimitRateThrottle` subclass |
| `DEFAULT_THROTTLE_RATES` | unchanged |
| `throttle_scope` on views | unchanged |

```python
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django.urls import path

from unilimit.integrations.drf import UniLimitScopedRateThrottle, UniLimitUserRateThrottle

REST_FRAMEWORK = {
    # Before: ["rest_framework.throttling.UserRateThrottle"]
    "DEFAULT_THROTTLE_CLASSES": ["unilimit.integrations.drf.UniLimitUserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "1000/day", "contacts": "20/day"},
}


class Contacts(APIView):
    # Before: [ScopedRateThrottle]
    throttle_classes = [UniLimitScopedRateThrottle]
    throttle_scope = "contacts"

    def get(self, request: Request) -> Response:
        return Response([])


urlpatterns = [path("contacts", Contacts.as_view())]
```

## Never both

Do not leave a built-in throttle and its UniLimit counterpart on the same scope: every
request would be counted twice. UniLimit refuses it. A view that throttles a scope with
both raises `ImproperlyConfigured` naming the scope, and `manage.py check` reports a
`DEFAULT_THROTTLE_CLASSES` that lists built-in throttle classes next to UniLimit ones (with
`"unilimit.integrations.django"` in `INSTALLED_APPS`).

## What changes

- **Rates.** `"100/day"`, `"3/min"` and the other DRF forms mean the same. Like DRF,
  only the first letter of the period counts, so `"1/month"` means one per *minute*;
  `manage.py check` warns about it. Rates DRF cannot parse, such as `"5/30s"` or
  `"10/second;1000/day"`, use UniLimit's grammar.
- **Algorithm.** DRF uses a timestamp list in the cache (a sliding log). UniLimit's
  default is a sliding window counter, and `algorithm = "fixed_window"` or
  `"token_bucket"` are class attributes of `UniLimitRateThrottle` subclasses.
- **Counters** live in Redis, are exact across workers and processes, and are not
  affected by your Django cache configuration.
- **`NUM_PROXIES`** is not used. Configure `trusted_proxies` on the `Limiter`.
- **`Retry-After`** comes from the exact counter state.
- **Custom `get_cache_key`** becomes a `key` function of the request context; see the
  [quickstart](quickstart.md#custom-identities).
- **Redis down.** DRF's cache-based throttles raise whatever your cache raises.
  UniLimit follows the limiter's `fail_mode`: open by default, or 503 when closed.

## Supported DRF versions

DRF 3.16 and later. See [Django supported versions](../django/versions.md).
