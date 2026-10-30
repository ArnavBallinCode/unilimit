# Comparing django-ratelimit and UniLimit for Django & DRF

[django-ratelimit](https://github.com/jsocol/django-ratelimit) has long been the standard
rate-limiting package in the Django ecosystem. However, it was built prior to Django's async
revolution (ASGI) and lacks native support for modern API architectures, distributed token
buckets, and Django REST Framework throttle grouping.

---

## Quick Comparison Table

| Capability | django-ratelimit | UniLimit |
|---|---|---|
| **ASGI / Async View Safety** | ❌ Synchronous cache calls inside async handlers | ✅ **Full ASGI lifespan and non-blocking async execution** |
| **Django REST Framework (DRF)** | ❌ Separate from DRF throttling; risks double-counting | ✅ **First-class DRF throttle classes** (`UniLimitAnonRateThrottle`, `UniLimitUserRateThrottle`, `UniLimitScopedRateThrottle`) |
| **Atomic Multi-Rule Batches** | ❌ Independent cache keys incremented separately | ✅ **Single atomic Lua transaction** (all-or-nothing) |
| **Modern Algorithms** | ❌ Fixed window only | ✅ **Sliding Window Counter, Token Bucket, Fixed Window** |
| **Storage Privacy** | ❌ Raw IPs or keys hashed with simple SHA-256 | ✅ **Keyed HMAC-SHA256 digests** with secret salting |
| **Redis Cluster Support** | ❌ No slot awareness | ✅ **Native hash slot tagging** (`{digest}`) |
| **Observability** | ❌ None | ✅ **Prometheus**, **OpenTelemetry**, and throttled logging |
| **Reverse Proxy Security** | ⚠️ Checks client headers directly | ✅ **Right-to-left `TrustedProxies` validation**; halts on spoofing |

---

## 1. True ASGI & Async Parity

Django applications increasingly run on ASGI servers (Uvicorn, Daphne) to handle WebSockets,
server-sent events, and high-concurrency async views. When `django-ratelimit` executes against
a cache backend (like Redis), it performs blocking I/O directly in the calling thread, blocking
the ASGI event loop.

UniLimit detects ASGI vs WSGI environments automatically:
- In ASGI, it uses `redis.asyncio` and `await request.auser()` without database query blocking.
- In WSGI, it uses thread-safe connection pools.

---

## 2. Django REST Framework Coherence

`django-ratelimit` operates independently of DRF's built-in throttling system. If developers use
both, requests are counted twice or quotas drift out of sync.

UniLimit provides drop-in throttle classes that replace DRF's default throttles:
```python
from rest_framework.views import APIView
from unilimit.integrations.drf import UniLimitUserRateThrottle


class OrderListView(APIView):
    throttle_classes = [UniLimitUserRateThrottle]
    # Uses standard DRF DEFAULT_THROTTLE_RATES settings:
    # 'user': '1000/day'
```

---

## 3. Migration Guide

### Drop-in Compatibility Shim
```python
# Change this:
# from django_ratelimit.decorators import ratelimit

# To this:
from unilimit.compat.django_ratelimit import ratelimit


@ratelimit(group="contact", key="ip", rate="5/m", block=True)
def contact_view(request):
    return HttpResponse("ok")
```
