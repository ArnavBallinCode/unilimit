# Changelog

All notable changes to **UniLimit** will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

- **`unilimit[starlette]` without FastAPI.** `unilimit.integrations.starlette` imported
  the FastAPI package and failed with `ImportError` unless FastAPI was installed. The
  Starlette-only modules now live in `unilimit.integrations.starlette`; the FastAPI
  package re-exports them, so existing imports keep working. CI installs the
  `starlette` extra alone.
- **SlowAPI adapter (`unilimit.compat.slowapi`).** `default_limits` and
  `application_limits` are enforced by the new `SlowAPIMiddleware` /
  `SlowAPIASGIMiddleware`; `headers_enabled` adds `X-RateLimit-*` headers; `exempt`,
  `shared_limit`, `per_method`, `error_message`, `exempt_when`, callable `cost` and
  callable limit values (per request) work; `enabled`, `strategy`, `retry_after`,
  `storage_options` and `in_memory_fallback_enabled` are honoured. An endpoint without a
  `request` parameter now fails at import instead of silently going unlimited. Stacked
  limits on one endpoint and multi-rate strings are one atomic batch.
- **django-ratelimit adapter (`unilimit.compat.django_ratelimit`).** Callable and
  dotted-path keys, `get:`/`post:`/`header:` keys (empty values share one bucket, as
  upstream), callable, dotted-path and tuple rates (a `None` rate leaves the request
  unlimited), `ALL`/`UNSAFE`, `request.limited` across stacked decorators, upstream
  default group names and rate grammar, async views and async class-based views.
  Stacked decorators with one group but different rates no longer share a counter.

### Changed

- SlowAPI adapter: `headers_enabled` defaults to `False`, as in SlowAPI.
  `RateLimitExceeded` is a Starlette `HTTPException` and carries the status (429, 403 for
  a missing key, 503 when failing closed) and headers. `limit()` takes SlowAPI's
  positional order. Unsupported options (`moving-window`, `key_style="url"`,
  `swallow_errors`, `in_memory_fallback`, `key_prefix`, `auto_check`, `config_filename`)
  raise `ConfigurationError` instead of being ignored. The 429 body reads
  `"Rate limit exceeded: 5 per 1 minute"`.
- Both adapters name counters `"{endpoint or group}:{rate}"`; counters stored by 0.1.1
  adapters start fresh.
- django-ratelimit adapter: `rate=` is required (it defaulted to `10/minute`), and the
  unused `empty_to` argument is removed.

### Documentation and adoption

- Tested migration guides: [from SlowAPI](docs/fastapi/migrating-from-slowapi.md) and
  [from django-ratelimit](docs/django/migrating-from-django-ratelimit.md), each listing
  supported options and behavioural differences.
- Added a tested, Redis-free README quickstart, beginner concepts, deployment and
  migration guides, a documentation index, and contribution/issue-report guidance.
- Corrected Redis installation instructions, `identity_secret` configuration, BLAKE2b
  digest descriptions, and atomicity/privacy claims. Added executable public-example
  coverage and documentation-wide local-link/navigation checks.
- Replaced unsupported competitor claims with dated upstream sources. Compatibility
  adapter differences are listed in [the migration guide](docs/migration.md).
- Corrected assistant context and removed missing asset/page references from documentation
  navigation. Hosted documentation deployment remains unverified.

## [0.1.1] - 2026-10-31

### Added
- **AI Agent-Ready Documentation Standard (`llms.txt` and `llms-full.txt`):** Structured, lightweight context files at repository root following the `llms.txt` specification for seamless ingestion by Cursor, Windsurf, Claude Code, GitHub Copilot, and LLMs.
- **Partial Compatibility Adapters (`unilimit.compat`):**
  - `unilimit.compat.slowapi`: Basic decorator and exception-handler adapter. Does not
    implement all SlowAPI options or application-wide enforcement.
  - `unilimit.compat.django_ratelimit`: Basic synchronous decorator adapter. Callable keys
    and async views are not supported correctly; see [migration](docs/migration.md).
- **Agent Prompts & Rules Reference (`docs/ai_agents.md`):** Ready-to-paste system instructions for `.cursorrules`, `.github/copilot-instructions.md`, and `CLAUDE.md` to provide project-specific integration context.
- **Agentic SEO & Technical Comparisons:**
  - `docs/comparisons/slowapi-vs-unilimit.md`: Detailed technical comparison against SlowAPI.
  - `docs/comparisons/django-ratelimit-vs-unilimit.md`: Detailed technical comparison against django-ratelimit.
  - `docs/comparisons/limits-vs-unilimit.md`: Detailed architectural comparison against limits / coredis.
- **Modern Auto-Documentor Integration:**
  - Evaluated documentation generators (Mintlify, Fumadocs, Docusaurus, MkDocs Material).
  - Added a Mintlify navigation configuration (`mint.json`); this alone does not provision or validate hosted documentation.
  - Added auto-documentor guide (`docs/autodocumentors.md`).

---

## [0.1.0] - 2026-10-31

### Initial Release

UniLimit is a unified rate-limiting and connection concurrency library for Python with a
shared core across FastAPI, Starlette, Django, Django REST Framework, and plain Python.

### Features

#### Core & Algorithms
- **Algorithms:** Fixed window, sliding window counter, and token bucket with sync and async parity.
- **Atomic Batches:** All-or-nothing evaluation in memory, standalone Redis, and a single Cluster slot. Cross-slot batches can partially consume quota.
- **Sans-I/O Architecture:** Rule compilation and policy evaluation are decoupled from storage execution.
- **Key Derivation & Privacy:** BLAKE2b-128 identity digests, keyed when `identity_secret` is configured. Raw client identities, tokens, and IP addresses are never stored in backend keys or leaked in logs.
- **In-Memory Storage:** Thread-safe, high-performance in-memory backend for development, testing, and single-process applications.

#### Redis Backend
- **Lua Parity:** Lua batch evaluation (`batch.lua`) is a line-by-line equivalent of the pure Python reference implementation.
- **Topology Support:** Standalone Redis, Redis Cluster, and Redis Sentinel (`sentinel://` URLs and `RedisBackend.from_sentinel`).
- **TLS / SSL:** Full support for encrypted connections via `rediss://` and customizable SSL contexts.
- **Cluster Slot Pinning:** Hash tags `{digest}` ensure all rate-limit keys for an identity map to the same cluster slot.
- **Resilience:** Circuit breaker with fail-fast cooldown, connection pooling caps, and non-blocking async execution via `redis.asyncio`.

#### WebSocket Concurrency & Messaging
- **Distributed Concurrency Limits:** `WebSocketConnectionLimit` and `ConcurrencyLimit` cap concurrent open connections per user or IP across distributed worker fleets.
- **Connection Leases:** Backed by Redis Sorted Sets with automatic microsecond TTLs.
- **Crash Recovery:** Automatic background heartbeat renewal; stale leases auto-expire after worker crashes.
- **Weighted Budgets:** `WebSocketRateLimit` supports message costs (`cost: int = 1`) and configurable rejection policies (drop, error frame, close, raise).

#### Framework Integrations
- **FastAPI:** Lifespan setup, middleware, and `RateLimit` dependency injection with standards-compliant headers (`X-RateLimit-*`, `Retry-After`).
- **Starlette:** Standalone ASGI middleware and WebSocket limiters without FastAPI dependency.
- **Django:** ASGI and WSGI middleware, `@ratelimit` decorator supporting sync and async views, and automatic ASGI lifespan cleanup.
- **Django REST Framework:** Drop-in throttle replacements (`UniLimitAnonRateThrottle`, `UniLimitUserRateThrottle`, `UniLimitScopedRateThrottle`).
- **Anti-Spoofing & Proxies:** `TrustedProxies` with right-to-left forwarding header parsing and IPv6 `/64` subnet aggregation.

#### Observability & Diagnostics
- **Structured Telemetry:** Frozen typed events (`DecisionEvent`, `BackendUnavailableEvent`, `BreakerEvent`, `FallbackEvent`) with zero sensitive data.
- **Exporters:** First-party Prometheus (`unilimit[prometheus]`) and OpenTelemetry (`unilimit[otel]`) metric observers.
- **Local Fallback:** Opt-in in-memory fallback during Redis outages to protect availability.
- **Throttled Failure Logging:** Process-local rate-limited logging to prevent disk and log service saturation.

#### Extensibility
- **Backend Protocols:** `SyncBackend`, `AsyncBackend`, `LeaseBackend`, and `AsyncLeaseBackend`.
- **Conformance Harness:** Reusable test harness in `unilimit.testing` to verify custom backends against all invariant and algorithm requirements.
