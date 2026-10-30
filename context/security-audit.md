# Security & Reliability Audit Report

_Conducted: Milestone 9 (Pre-Release Audit)_

This document records the formal security and reliability review of UniLimit prior to public
release on PyPI.

---

## 1. Scope of the Review

The audit covered the complete core engine, algorithms, backends, and framework integrations:
- `src/unilimit/core/` (Identity resolution, hashing, rules, limiter, policy)
- `src/unilimit/algorithms/` (Fixed window, sliding window, token bucket)
- `src/unilimit/backends/redis/` (Lua scripts `batch.lua` and `leases.lua`, Redis connection pooling, Sentinel, Cluster routing, failure handling)
- `src/unilimit/backends/memory.py` (Thread-safe memory backend and local fallback)
- `src/unilimit/integrations/` (FastAPI, Starlette, Django, DRF)
- `src/unilimit/observability.py` (Events, metrics, logging)

---

## 2. Component-by-Component Audit Findings

### 2.1 Identity Resolution & Anti-Spoofing (`core/identity.py`)
- **Verification:**
  - `TrustedProxies` requires explicit network definition. If an empty list or invalid CIDR is provided, `ConfigurationError` is raised at initialization.
  - A warning is emitted if `0.0.0.0/0` is trusted.
  - Headers are inspected only when the socket peer is within `trusted.networks`.
  - Traversal is strictly right-to-left. First untrusted entry is returned.
  - Malformed IP inputs (e.g. invalid syntax, embedded semicolons, non-ASCII characters, obfuscated IDs) immediately stop traversal and default to the last verified hop.
  - IPv6 aggregation (`/64` default) prevents Sybil rotation attacks across large IPv6 allocations.
  - String length cap: `MAX_IDENTITY_LENGTH = 1024` prevents memory bloat attacks via large headers.

### 2.2 Storage Keys & Cryptographic Isolation (`core/keys.py`)
- **Verification:**
  - Keys use HMAC-SHA256 (`identity_digest`) with optional secret salt.
  - Invariant 5 holds: No raw IP, user ID, API key, or tenant value is ever placed into storage keys.
  - Kind separation: `(kind, value)` ensures an IP "1.1.1.1" and a user ID "1.1.1.1" produce different keys.
  - Redis Cluster slot pinning: Hash tags `{digest}` ensure all rate limits for a given identity hash to the same cluster slot.
  - Header keys RFC 9110 token validation prevents special characters in Redis key segments.

### 2.3 Lua Script Safety & Redis Backends (`backends/redis/`)
- **Verification:**
  - Lua scripts (`batch.lua` and `leases.lua`) execute atomically without race conditions.
  - No dynamic code evaluation: scripts use parameterized `KEYS` and `ARGV`.
  - All operations in a batch commit together; if any limit fails, nothing is committed (Invariant 1).
  - Floating-point calculations: strictly ordered using standard Lua arithmetic matching Python `algorithms/`.
  - TTL enforcement: Every updated key is tagged with a microsecond TTL (`PEXPIRE`), preventing orphaned keys.
  - Error categorization: Network disconnects and timeouts raise `BackendUnavailable` and honor `fail_mode`. Authentication errors (`NOAUTH`, `WRONGPASS`) raise immediately and fail closed (Invariant 6).

### 2.4 WebSocket Connection Leases (`backends/redis/leases.lua` & `fastapi/websockets.py`)
- **Verification:**
  - Leases are stored in Redis Sorted Sets (`ZSET`) with expiration timestamp scores.
  - Expired leases are cleaned automatically on each acquire/renew call.
  - Automatic background heartbeats renew active leases before TTL expiration.
  - On worker crash, leases auto-expire when their TTL elapses, preventing permanent lease starvation.

### 2.5 Observability & Telemetry Privacy (`observability.py`)
- **Verification:**
  - Invariant 11 & 12 hold: Telemetry construction is isolated. Observer exceptions are swallowed and never affect rate limit enforcement.
  - Zero sensitive attributes: Events and Prometheus labels never contain client identities, IPs, request paths, or query parameters.
  - Throttled failure logger suppresses duplicate logs per category, preventing log flooding and disk exhaustion during Redis outages.

---

## 3. Residual Risks & Accepted Tradeoffs

1. **Redis Cluster Multi-Slot Batches:**
   - When a batch contains rules with different identities (very rare in standard HTTP handlers, where all rules apply to the request identity), keys may map to different Redis slots. In this case, Redis Cluster executes individual slot operations, which can result in `partial_commit` if one slot rejects. This is documented in `ARCHITECTURE.md` and surfaced via `Decision.partial_commit`.
2. **Local Fallback Worker Scaling:**
   - During a Redis outage, the opt-in in-memory fallback operates on a per-process basis. Therefore, an application running $N$ worker processes may admit up to $N \times \text{limit}$ requests during the outage window. This is an explicit, documented design tradeoff (Invariant 12).
3. **DRF Synchronous Throttles:**
   - DRF throttles run synchronously. Under ASGI, UniLimit runs them on a thread executor using the sync Redis client to avoid blocking the main event loop.

---

## 4. Audit Conclusion

The UniLimit codebase adheres to all architectural invariants. Security controls for proxy trust, identity redaction, cryptographic key derivation, DoS protection, and fail-mode semantics are comprehensive, rigorously tested, and ready for public release.
