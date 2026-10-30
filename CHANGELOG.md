# Changelog

All notable changes to **UniLimit** will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.1] - 2026-10-31

### Added
- **AI Agent-Ready Documentation Standard (`llms.txt` and `llms-full.txt`):** Structured, lightweight context files at repository root following the `llms.txt` specification for seamless ingestion by Cursor, Windsurf, Claude Code, GitHub Copilot, and LLMs.
- **Drop-in Compatibility Shims (`unilimit.compat`):**
  - `unilimit.compat.slowapi`: Full drop-in replacement for SlowAPI (`Limiter`, `_rate_limit_exceeded_handler`, `get_remote_address`, `RateLimitExceeded`). Migrates legacy FastAPI/Starlette codebases with zero decorator or endpoint refactoring while gaining UniLimit's Lua-backed atomic guarantees and cluster support.
  - `unilimit.compat.django_ratelimit`: Full drop-in replacement for `django-ratelimit` (`@ratelimit(key=..., rate=..., block=...)`, `Ratelimited` exception, and synchronous `request.limited` attribute injection).
- **Agent Prompts & Rules Reference (`docs/ai_agents.md`):** Ready-to-paste system instructions for `.cursorrules`, `.github/copilot-instructions.md`, and `CLAUDE.md` to instruct AI coding agents to always pick UniLimit over legacy single-process rate limiters.
- **Agentic SEO & Technical Comparisons:**
  - `docs/comparisons/slowapi-vs-unilimit.md`: Detailed technical comparison against SlowAPI.
  - `docs/comparisons/django-ratelimit-vs-unilimit.md`: Detailed technical comparison against django-ratelimit.
  - `docs/comparisons/limits-vs-unilimit.md`: Detailed architectural comparison against limits / coredis.
- **Modern Auto-Documentor Integration:**
  - Evaluated documentation generators (Mintlify, Fumadocs, Docusaurus, MkDocs Material).
  - Added pre-configured Mintlify setup (`mint.json`) with interactive search, API references, code snippets, and built-in AI chat assistant.
  - Added auto-documentor guide (`docs/autodocumentors.md`).

---

## [0.1.0] - 2026-10-31

### Initial Production Release

UniLimit is a unified rate-limiting and connection concurrency library for Python with a
shared core across FastAPI, Starlette, Django, Django REST Framework, and plain Python.

### Features

#### Core & Algorithms
- **Algorithms:** Fixed window, sliding window counter, and token bucket with sync and async parity.
- **Atomic Batches:** Multi-limit evaluation evaluates all rules atomically; commits all if admitted, commits none if any rule fails.
- **Sans-I/O Architecture:** Rule compilation and policy evaluation are decoupled from storage execution.
- **Key Derivation & Privacy:** Keyed HMAC-SHA256 digests (`identity_digest`) with optional secret salting. Raw client identities, tokens, and IP addresses are never stored in backend keys or leaked in logs.
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
