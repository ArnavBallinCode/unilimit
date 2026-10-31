# Migrating to UniLimit

Prefer the native framework integrations for new code. For existing SlowAPI and
django-ratelimit code, compatibility adapters let you change imports first and rewrite
later. They accept the upstream options they can honour and raise `ConfigurationError`
for the rest, so nothing is silently ignored; the behavioural differences are listed in
each guide. Existing stored counters are not migrated.

## From SlowAPI

Follow [migrating from SlowAPI](fastapi/migrating-from-slowapi.md): change the imports
to `unilimit.compat.slowapi`, keep `@limiter.limit(...)`, `shared_limit`, `exempt`,
`default_limits` and `application_limits` (with `SlowAPIMiddleware`), and review what
changes: atomic batches, per-endpoint counters, the default algorithm and empty keys.

To move to the native API afterwards, replace each decorator with
`Depends(RateLimit(...))` and call `setup(app, limiter)`; see the
[complete example](fastapi/quickstart.md). A positional string is a rule name; use
`rate="10/minute"` for inline rates. Named rules share counters unless you opt into
per-route separation. Authentication identities should use the
[identity dependency](fastapi/authentication.md). `default_limits` become `global_rules`,
which apply to every route and are additive: a route rejection does not refund global
consumption.

## From django-ratelimit

Follow [migrating from django-ratelimit](django/migrating-from-django-ratelimit.md):
configure `UNILIMIT`, change the imports to `unilimit.compat.django_ratelimit` and keep
your `@ratelimit(group=, key=, rate=, method=, block=)` decorators, including callable
and dotted-path keys and rates, `request.limited` and async views. Blocked requests get
a 429 response rather than `Ratelimited`.

To move to the native [`@ratelimit` decorator](django/decorators.md) afterwards, note
that a positional string there is a rule name and rates use `rate=`. Select
`algorithm="fixed_window"` if you need fixed-window behaviour; UniLimit defaults to a
sliding window counter. Native `action="log"` replaces `block=False`.

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
