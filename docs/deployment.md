# Deployment checklist

## 1. Choose shared storage deliberately

Use Redis when several workers must share a budget. Install the Redis extra alongside
your framework, for example `pip install "unilimit[fastapi,redis]"`. Configure a namespace
per application. Memory storage and `fallback="memory"` are process-local. Live memory
entries can be evicted at capacity, resetting their budgets; monitor `live_evictions`.

Standalone Redis and single-slot Cluster batches are atomic. Cross-slot batches can
report `partial_commit`: some quota may be consumed even though the request is rejected.
Global middleware and route/decorator/DRF stages are separate batches.

## 2. Establish trustworthy identities

Authenticate users before resolving user quotas. Use DRF throttles for DRF token/JWT
users, and an explicit identity dependency in FastAPI. Configure only your actual proxy
networks with `TrustedProxies`; confirm your server's own proxy handling preserves the
peer information the limiter needs. Test with spoofed forwarding headers.

Storage identity digests use BLAKE2b-128. Set `identity_secret` consistently across workers
for keyed digests; without it the digest is unkeyed and enumerable for predictable
identities. Load secrets from your deployment's secret store. Rotating the secret changes
counter keys and therefore resets effective quotas. Never put identities in rule names.
See [security](security.md) and [identity examples](fastapi/identity.md).

## 3. Choose outage behavior

The default `fail_mode="open"` permits requests when the backend is unavailable.
Use `fail_mode="closed"` for rules that must reject during an outage (HTTP 503).
Authentication failures and corrupt backend state raise rather than silently failing open.
Optional local fallback tightens fail-open only; N workers can each admit their own budget,
and local counters are not merged into Redis on recovery.

## 4. Close clients and observe behavior

FastAPI `setup()` owns shutdown by default. For Django ASGI, use the documented
[lifespan wrapper](django/redis.md). Plain Python applications close their limiter with
`close()` / `aclose()`. Do not create a limiter and connection pool per request.

Watch allowed/rejected decisions, backend errors, fallback activation, and latency with
[metrics](observability/metrics.md). Observers run inline, so keep them fast. Keep rule
names bounded to avoid unbounded metric labels.

## 5. Roll out with evidence

Start in staging with representative identities and worker counts. Exercise quota
exhaustion, recovery, a Redis outage, and authentication failure. Use `Rule(action="log")`
to observe would-be violations before enforcing where appropriate. Measure p95/p99
latency and rejected-request behavior in your own application; the published laptop
benchmarks are not a capacity guarantee. Keep edge protection for traffic that could
exhaust network or server capacity before application code runs.
