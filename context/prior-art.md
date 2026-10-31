# Prior art and borrowing ledger

UniLimit learns from existing Python rate limiters. This file records what each one
does well, where UniLimit differs on purpose, and **everything adopted from them**:
ideas or code, with licences.

## Current comparison review

See the [2026-11-01 adoption audit](adoption-audit.md) and
[user comparisons](../docs/comparisons/README.md) for current selection guidance.
The ledger below records historical design inputs, not a version-independent capability
matrix. In particular, current FastAPI-Limiter documentation uses PyrateLimiter, and
SlowAPI already supports costs and in-memory fallback. No competitor code was copied
for the documentation audit.

## Projects

| Project | Licence | Copyright | Strengths | Weaknesses UniLimit avoids |
|---|---|---|---|---|
| [limits](https://github.com/alisaifee/limits) | MIT | © Ali-Akber Saifee | Strategy/storage split; rate grammar; many storages; moving and sliding windows | No request/identity layer; multiple limits are independent hits (earlier limits stay consumed); async Redis supports coredis or redis-py |
| [slowapi](https://github.com/laurentS/slowapi) | MIT | © Laurent Savaete | Decorator UX; default/application/shared limits; `in_memory_fallback`; `exempt_when`; `cost`; HTTP-date `Retry-After` | Calls the sync `limits` API from async code (blocks the loop); decorator needs a `request` parameter; `redis<4` extra |
| [django-ratelimit](https://github.com/jsocol/django-ratelimit) | Apache-2.0 | © James Socol | Any Django cache; `user_or_ip`; `header:`/`get:`/`post:` keys; IPv4/IPv6 masks; `block=False` | Fixed window only; sync only; rate in the cache key (config changes reset counters); decorator runs before DRF auth |
| [fastapi-limiter](https://github.com/long2ice/fastapi-limiter) | Apache-2.0 | long2ice (author) | Dependency-based API; WebSocket limiter | Trusts the first `X-Forwarded-For` entry blindly; independent limiters per dependency |

Licence obligations when copying code: **MIT** — keep the copyright and permission
notice. **Apache-2.0** — keep the licence and any NOTICE, and mark modified files.
Copied code is listed in `THIRD_PARTY_NOTICES.md` (none so far).

## Adopted

All adoptions so far are **re-implementations of ideas or behaviour**; no source code has
been copied. Each is written to fit UniLimit's architecture: atomic batches,
sync/async parity, sans-I/O policy.

| What | From | Where in UniLimit | Notes |
|---|---|---|---|
| Rate string grammar (`"10 per minute"`, `"10/minute"`) | limits | `core/rate.py` | Extended with compact windows (`"5/30s"`) |
| Multi-limit strings `"10/second;100/day"` (separators `;` `,` `|`) | limits / slowapi | `Rate.parse_many`, `Rule.many`, inline `RateLimit(rate=…)` | **Improvement:** the rules are consumed atomically. Tested for equality with the real `limits.parse_many` |
| `month` = 30 days, `year` = 360 days | limits | `core/rate.py` | Kept identical so migrated strings mean the same thing |
| `user_or_ip` key | django-ratelimit | `core/identity.py` | Generalised into fallback chains |
| Hashed storage keys | django-ratelimit | `core/keys.py` | Keyed BLAKE2b with kind separation |
| IPv6 /64 aggregation | django-ratelimit (`IPV6_MASK`) | `core/identity.py` | |
| `cost` (weighted requests), `exempt_when` | slowapi | `Rule.cost`, `Rule.when` | |
| `block=False` | django-ratelimit | `Rule(action="log")` (shadow mode) | |
| `Retry-After` as an HTTP date | slowapi (`retry_after="http-date"`) | `ResponseConfig.retry_after_format` | |
| WebSocket limiter | fastapi-limiter (`WebSocketRateLimiter`) | `integrations/fastapi/websocket.py` | **Improvement:** limits both the handshake (close 1008) and messages, with atomic multi-rate rules |
| Dependency-based FastAPI API | fastapi-limiter | `RateLimit` | **Improvement:** `identity=` dependencies, dedupe with middleware |
| `in_memory_fallback` | slowapi | `Limiter(fallback=)` | Idea only, no code copied. **Improvements:** explicit, never silent (events, logs, `Decision.fallback`), only for fail-open rules so it never loosens fail-closed, sync and async, defined recovery semantics |
| `@ratelimit` view decorator, `key="header:X-Name"`, `IPV4_MASK`, `method=` filter | django-ratelimit | `integrations/django/decorators.py`, `core/identity.py`, `Limiter(ipv4_prefix=)` | Idea only, no code copied. **Improvements:** several rules per view in one atomic batch, async views without blocking, rate not part of the storage key, trusted-proxy IPs instead of `REMOTE_ADDR`/first `X-Forwarded-For` |
| Throttle classes keyed by scope with `DEFAULT_THROTTLE_RATES` | Django REST framework (BSD-3-Clause) | `integrations/drf/throttling.py` | Interface and settings conventions only, so migration is a class swap. **Improvements:** exact across processes, one atomic batch per view, refuses double counting |

## Planned adoptions

| What | From | Milestone | Design notes |
|---|---|---|---|
| `query:<param>`, `post:<field>` key sources | django-ratelimit | Later | Callable keys cover them today; `post:` needs body parsing |
| Response-based deduction (count only failed logins) | Flask-Limiter (`deduct_when`) | Later | Needs check-before / consume-after semantics; design carefully against atomicity |
| Global enable switch | slowapi (`enabled=`) | M6 | For tests and incidents |

## Deliberately not adopted

| What | Why |
|---|---|
| Trusting the first `X-Forwarded-For` entry (fastapi-limiter default, slowapi `get_ipaddr`) | Spoofable; UniLimit walks right to left through `TrustedProxies` |
| Sequential per-limit hits | Not atomic; earlier limits stay consumed on rejection |
| Sync storage calls from async endpoints | Blocks the event loop |
| Rate inside the storage key (django-ratelimit) | Changing a limit silently resets every counter |
| `override_defaults` (route limits replace global ones) | Global and route rules are separate, additive steps by design |
