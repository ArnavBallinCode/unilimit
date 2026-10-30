# Comparing limits and UniLimit

[limits](https://github.com/alisaifee/limits) is a foundational Python library providing rate-limiting
storage backends and strategies. UniLimit builds upon the good ideas in `limits` (such as the rate
grammar `"10/minute"`) while providing an integrated, production-ready framework architecture.

---

## Quick Comparison Table

| Capability | limits | UniLimit |
|---|---|---|
| **Multi-Rule Evaluation** | ❌ Independent hit calls (sequential) | ✅ **Atomic batches** (single Redis Lua call) |
| **Framework Integrations** | ❌ None (low-level building block) | ✅ **Batteries-included** for FastAPI, Starlette, Django, and DRF |
| **Connection Concurrency** | ❌ Rate limits only | ✅ **Native WebSocket connection capping** (`WebSocketConnectionLimit`) |
| **Storage Key Privacy** | ❌ Plaintext keys | ✅ **HMAC-SHA256 digests** (no IPs or user tokens in keys) |
| **Redis Cluster Safety** | ⚠️ Multi-keys span multiple slots | ✅ **Slot-pinned** per identity via `{digest}` hash tags |
| **Redis Topologies** | Standalone, Sentinel, Cluster | Standalone, Sentinel, Cluster, TLS |
| **Async Support** | Separate async backends (coredis/asyncio) | First-class sync and `redis.asyncio` with loop lifecycle handling |
| **Observability** | ❌ None | ✅ **Prometheus**, **OpenTelemetry**, and throttled logging |
| **Outage Fallback** | ❌ None | ✅ **Opt-in in-memory fallback** |

---

## Architecture Summary

While `limits` is an unopinionated library for evaluating single limits, UniLimit is an
end-to-end enforcement engine:
- It maps HTTP request contexts (headers, cookies, sessions, proxy chains) to identities.
- It calculates standards-compliant HTTP response headers (`X-RateLimit-*`, `Retry-After`).
- It renders framework rejection responses (429 Too Many Requests, 503 Service Unavailable).
- It tracks distributed WebSocket connection leases with automated heartbeats.
