# UniLimit documentation

Start with the [two-minute runnable example](../README.md#try-it-in-two-minutes).
New to backend development? Read [the concepts guide](concepts.md) first.

## Build your application

- [FastAPI](fastapi/quickstart.md): dependencies, middleware, and HTTP responses.
- [Django](django/quickstart.md): settings, middleware, and view decorators.
- [Django REST Framework](drf/quickstart.md): authenticated throttle scopes.
- [WebSockets](fastapi/websockets.md): message budgets and connection leases.
- [Migration](migration.md): from SlowAPI ([guide](fastapi/migrating-from-slowapi.md)),
  django-ratelimit ([guide](django/migrating-from-django-ratelimit.md)), DRF and others.
- Starlette without FastAPI: install `"unilimit[starlette]"` and use
  `unilimit.integrations.starlette` (middleware and WebSocket limits).

## Operate it

- [Deployment checklist](deployment.md): storage, identities, failure policy, and rollout.
- [Redis configuration](fastapi/redis.md) and [Django lifecycle](django/redis.md).
- [Security](security.md), [events](observability/events.md),
  [metrics](observability/metrics.md), and [fallback](observability/fallback.md).
- [Benchmarks](observability/benchmarks.md): methodology, results, and limitations.

## Evaluate and extend it

- [Compare alternatives](comparisons/README.md).
- [Public API](../API_DESIGN.md) and [architecture](../ARCHITECTURE.md).
- [Custom backends](custom_backends.md).
- [Contributing](../CONTRIBUTING.md) and [AI assistant context](../llms.txt).
