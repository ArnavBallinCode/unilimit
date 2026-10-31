# Migrating to UniLimit

Prefer the native framework integrations for new code. Compatibility adapters exist,
but are **partial adapters, not drop-in replacements**. Import changes alone do not
preserve all upstream behavior. Existing stored counters are not migrated.

## From SlowAPI

Move route decorators to FastAPI `Depends(RateLimit(...))` and call `setup(app, limiter)`.
See the [complete example](fastapi/quickstart.md). A positional string is a rule name;
use `rate="10/minute"` for inline rates. Named rules share counters unless you opt into
per-route separation. Authentication identities should use the
[identity dependency](fastapi/authentication.md).

The current `unilimit.compat.slowapi` adapter supports basic `limit`, key functions,
method filters, and integer costs on sync/async request handlers. Its `default_limits`
and `headers_enabled` constructor values are stored but do not implement upstream
application-wide enforcement or successful-response headers. Callable rate values are
evaluated at decoration time. It has no full `shared_limit`, exemption, fallback-option,
or middleware compatibility. A decorated callable without a Starlette `Request` is not
limited. Stacked decorators are separate consumes, not one atomic batch.

For native multi-rate atomicity, use one dependency's multi-rate `rate=` or several
rules in the same consume call. Review global limits separately: they are additive,
and a route rejection does not refund global consumption.

## From django-ratelimit

Configure `UNILIMIT` and middleware, then use the native
[`@ratelimit` decorator](django/decorators.md). Select `algorithm="fixed_window"` if you
need fixed-window behavior; UniLimit defaults to a sliding window counter. Test boundary
behavior because the two libraries need not align windows identically.

The `unilimit.compat.django_ratelimit` adapter is limited to basic synchronous views
and supported string keys. Callable keys are currently a placeholder returning no
identity; `empty_to` is unused, and the wrapper is not async-aware. Do not migrate those
cases through the adapter. Its exported `Ratelimited` exception does not reproduce the
native rejection response path. Native `action="log"` supports observing violations
without blocking, but test any `request.limited` behavior your old application relies on.

## From DRF built-in throttles

Use `UniLimitAnonRateThrottle`, `UniLimitUserRateThrottle`, or
`UniLimitScopedRateThrottle`, retaining the scope rates in `DEFAULT_THROTTLE_RATES`.
Follow the [DRF quickstart](drf/quickstart.md), including `UNILIMIT` settings.
Do not install a UniLimit throttle and a DRF built-in throttle on the same scope.
DRF rate parsing preserves its first-letter period convention: `1/month` means a minute
there. Use explicit supported rates and run `manage.py check`.

## From limits or fastapi-limiter

There is no general adapter for either. Map identities, algorithms, storage configuration,
exception handling, and lifecycle explicitly. `limits` moving-window logs and UniLimit
sliding-window counters are different algorithms. FastAPI-Limiter APIs vary by version;
its current upstream documentation uses PyrateLimiter, while older examples use Redis
initialization. Pin and inspect the version you are migrating.

## Validate the switch

Test exhausted budgets, separate users, shared scopes, multiple workers, spoofed headers,
outages, and recovery. Confirm returned status codes and headers with your clients. Start
with one endpoint, observe behavior, and then expand. See [deployment](deployment.md).
