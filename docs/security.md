# Security Policy & Hardening Guide

UniLimit is designed from the ground up for high-security, multi-tenant, and high-throughput
environments. Rate limiters sit directly at the front line of incoming network traffic and
are frequently subjected to denial-of-service attempts, header spoofing, credential stuffing,
and side-channel attacks.

This document outlines UniLimit's threat model, security architecture, and best practices
for deploying UniLimit safely in production.

---

## 1. Threat Model & Guarantees

UniLimit provides defenses against the following attack vectors:

| Threat Vector | Mitigation in UniLimit |
|---|---|
| **IP Spoofing via Headers** | `TrustedProxies` evaluates forwarding headers (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`) only if the direct TCP peer is trusted; walks right-to-left and halts at the first untrusted hop. |
| **Header Injection Attacks** | Malformed IP strings, parameter injections, or SQL/command syntax in proxy headers immediately halt header traversal and safely default to the last verified hop. |
| **Sensitive Data Exposure in Storage** | Storage keys use BLAKE2b-128 identity digests (keyed when `identity_secret` is set) (`identity_digest`). Raw usernames, passwords, API tokens, and IP addresses are **never** written to Redis or memory keys. |
| **Log & Repr Information Leakage** | `Identity.value` is excluded from dataclass `repr` and string representations (`repr=False`). Logs print redacted values only (`value redacted`). |
| **Telemetry & Metrics Cardinality Attacks** | Observability events and Prometheus metric labels contain bounded low-cardinality metadata (rule names, outcomes, algorithms). User identities and paths are never emitted as metric labels. |
| **Fail-Open Security Compromise** | While global traffic can default to fail-open (`fail_mode="open"`), critical authentication and billing endpoints can be independently locked down with `fail_mode="closed"`. Authentication or corrupt-data errors **always** fail closed and raise exceptions. |
| **Redis Connection & Resource Exhaustion** | Bounded connection pools (`max_connections`, `pool_timeout`) and fast timeouts (`socket_timeout=0.5`) prevent connection storms from degrading backend services. Redis TTLs use millisecond precision; algorithm timestamps use integer microseconds. |
| **IPv6 Subnet Rotation / Sybil Attacks** | IPv6 client addresses are aggregated by subnet (`/64` by default) so an attacker rotating billions of IPv6 addresses within a `/64` allocation cannot bypass limits. |

---

## 2. Hardening Client IP & Trusted Proxies

### Never Trust Forwarding Headers Blindly
By default, UniLimit ignores all proxy headers (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`)
and reads only the transport socket peer IP. This prevents attackers from spoofing their IP
address by supplying arbitrary headers:

```python
from unilimit import Limiter

# Default: reads the direct socket peer IP only; ignores all headers
limiter = Limiter.from_url("redis://localhost:6379/0")
```

### Configuring Trusted Proxies Safely
When running behind reverse proxies, load balancers, or CDNs (e.g. Cloudflare, AWS ALB,
Nginx), specify the exact CIDR networks of those proxies:

```python
from unilimit import Limiter, TrustedProxies

# Only trust requests whose immediate TCP peer is inside these subnets:
trusted = TrustedProxies(
    networks=["192.0.2.10/32"],  # replace with your actual proxy address
    header="X-Forwarded-For",
)

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    trusted_proxies=trusted,
    ipv6_prefix=64,  # Aggregate IPv6 /64 blocks (default)
    ipv4_prefix=32,  # Individual IPv4 addresses (default)
)
```

> [!WARNING]
> Never configure `TrustedProxies(["0.0.0.0/0"])` in an environment accessible directly
> over the public internet. This would allow any client to forge their IP address. Only
> trust `0.0.0.0/0` if your application is strictly hosted on a private VPC where all
> ingress is physical or cryptographic proxy traffic.

### How Right-to-Left Traversal Works
When a request arrives from a trusted proxy:
1. UniLimit parses the header entries from right to left (the order proxies append their addresses).
2. It verifies each hop against `trusted.networks`.
3. The **first untrusted address encountered** is determined to be the real client IP.
4. Any forged entries prepended by the client at the far left are ignored.
5. If a malformed entry is encountered, traversal stops immediately and the last trusted hop is used.

---

## 3. Cryptographic Storage & Key Isolation

### Key Construction & No Raw Identities
UniLimit storage keys follow a deterministic, versioned structure:
```text
{namespace}:{version}:rl:{rule}:{algorithm}:{{identity_digest}}
```

Example Redis key:
```text
shop:v1:rl:login:sw:{wracCrvVYOyWdePswde2qQ}
```

The `{identity_digest}` segment is a base64url-encoded BLAKE2b-128 digest computed from
`(kind, value)`.
- Raw identity strings are not stored in the key. Without `identity_secret`, predictable
  identities can still be recovered by enumeration; hashing is not encryption.
- The `{digest}` hash tag guarantees that in **Redis Cluster**, all rate-limit counters
  for a given identity map to the exact same hash slot, eliminating multi-slot transaction
  penalties for single clients.

### Providing a Secret Salt
To protect against pre-computed rainbow table attacks on low-cardinality identity spaces
(such as small IPv4 ranges or sequential user IDs), provide a cryptographically secure
secret salt:

```python
import os
from unilimit import Limiter

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    identity_secret=os.environ["RATELIMIT_SECRET_KEY"],  # e.g. 32+ bytes of high entropy
)
```

---

## 4. Fail-Open vs Fail-Closed Strategy

When the backend storage (Redis) is unreachable or times out, UniLimit applies its
`fail_mode` policy:

### General Recommendation: Defense-in-Depth
- **Public browsing routes:** Use `fail_mode="open"` (the default). If Redis experiences
  a brief network blip, legitimate users continue to browse without disruption.
- **Critical / high-abuse routes (Login, Password Reset, Billing, SMS/Email dispatch):**
  Configure `fail_mode="closed"`. If Redis is down, these operations reject with 503
  to prevent credential stuffing or financial abuse during an outage.

```python
from fastapi import Depends, FastAPI
from unilimit import Limiter, Rule
from unilimit.integrations.fastapi import RateLimit, setup

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    fail_mode="open",  # Global default
    global_rules=[Rule("site", "300/minute", fail_mode="open")],
)

app = FastAPI()
setup(app, limiter)


# Login endpoint is protected: fails CLOSED if Redis is unavailable
@app.post(
    "/api/login",
    dependencies=[Depends(RateLimit("login", rate="5/minute", fail_mode="closed"))],
)
async def login() -> dict[str, str]:
    return {"status": "ok"}
```

> [!IMPORTANT]
> Authentication failures (e.g. invalid Redis credentials, Redis `NOAUTH` or `WRONGPASS`)
> and configuration errors **never** fail open. They always raise an exception. Only
> network connectivity or cluster partition errors are governed by `fail_mode`.

---

## 5. Denial-of-Service & Resource Bounds

1. **Identity Value Length:** UniLimit caps identity values at `MAX_IDENTITY_LENGTH = 1024`
   characters. Excessively long strings provided in headers or tokens are rejected and
   coerced to `None` to prevent memory exhaustion attacks.
2. **Redis Memory Management:** Every rate-limit counter and lease key written to Redis
   has an automatic TTL set via `PEXPIRE`. Unused keys expire automatically. We recommend
   using a dedicated Redis instance with a memory budget and alerts. Eviction policies
   such as `volatile-lru` or `allkeys-lru` can remove live counters and admit extra traffic.
   With `noeviction`, writes can fail when full; capacity and error handling must be tested.
3. **Connection Pool Bounds:** Configure `max_connections` (default 32) and `pool_timeout`
   (default 1.0s). During connection spikes, excess requests wait up to `pool_timeout`
   rather than spawning hundreds of concurrent TCP connections.

---

## 6. Observability & Information Redaction

1. **Logs:** The internal logger uses a throttled logger (`unilimit._logging.throttled`)
   which suppresses duplicate error logs per category and redacts identity values.
2. **Metrics:** Prometheus exporters (`unilimit[prometheus]`) strictly use bounded
   labels: `reason`, `source`, `rule`, `backend`, and `exception`. Client identities,
   IPs, paths, and URLs are never exported as labels, eliminating metric cardinality
   explosions in Prometheus or Datadog.
3. **HTTP Response Headers:** Rate limit response headers (`X-RateLimit-Limit`,
   `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`) expose only quota
   integers and standard timestamps. Internal Redis keys or cluster details are never
   disclosed.
