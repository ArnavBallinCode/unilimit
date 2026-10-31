# django-ratelimit and UniLimit

Reviewed 2026-11-01. [django-ratelimit](https://django-ratelimit.readthedocs.io/en/stable/)
fits Django projects that want decorators using their existing cache infrastructure.
Its [usage guide](https://django-ratelimit.readthedocs.io/en/stable/usage.html) documents
IP/user keys, custom keys, method filtering, and non-blocking annotation of requests.

## Deployment differences

The upstream [security guide](https://django-ratelimit.readthedocs.io/en/stable/security.html)
explicitly warns about spoofed forwarding headers and leaves proxy normalization to the
deployment. It does not blindly trust forwarding headers by default. UniLimit instead
provides a configurable trusted-proxy resolver. Both require correct deployment setup.

django-ratelimit requires a suitable shared cache with atomic increment support.
UniLimit supplies memory and Redis backends, sync/async integration, three algorithms,
and DRF throttles that resolve identities after DRF authentication. Multiple native rules
can be evaluated as a single batch, subject to the Cluster and middleware boundaries in
[the comparison overview](README.md).

## Migration

Use the [Django quickstart](../django/quickstart.md) for native decorators or the
[DRF guide](../drf/quickstart.md) for token-authenticated APIs. To change imports first,
follow the [django-ratelimit adapter guide](../django/migrating-from-django-ratelimit.md):
it supports callable, dotted-path and accessor keys, dynamic rates, `block=False` and
async views, and lists the algorithm, status code and identity differences.
