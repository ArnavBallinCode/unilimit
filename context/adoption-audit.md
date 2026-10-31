# Adoption audit — 2026-11-01

## Evidence and scope

Reviewed the local implementation, public examples, compatibility adapters, benchmark
report, upstream docs/source, and the authenticated owner's repository inventory (130
repositories returned; 72 public non-forks). No other repository was modified and no
outreach was sent. Private repository details are intentionally omitted here.

The GitHub release API reports `v0.1.1` published on 2026-10-30. The repository About
metadata has no homepage or topics. Release existence does not establish production
adoption or independently verify PyPI installation.

## Findings addressed in this change

- The README's Redis examples followed installs without the Redis extra. It now starts
  with an executable single-process demo and explains when to switch to Redis.
- README/assistant examples used nonexistent WebSocket constructor options and omitted
  DRF scope configuration. The front page now links to existing framework recipes;
  assistant context uses a tested example rather than invented signature summaries.
- Security documentation used `secret=` instead of `identity_secret=` and described
  HMAC-SHA256 instead of BLAKE2b-128. It also overstated digest irreversibility and
  recommended eviction without explaining that live quota can reset.
- Comparisons incorrectly denied SlowAPI costs/fallback and limits redis-py async support.
  They overstated UniLimit's atomicity and general compatibility. Rewritten with sources,
  strengths of alternatives, transaction boundaries, and measured performance caveats.
- Native integration docs had executable coverage; the README and assistant/security
  entry points did not. New tests execute those examples and check HTTP decisions,
  identity isolation, Redis configuration, and local links across all documentation.
- Added beginner concepts, deployment and migration guidance, a documentation index,
  contribution instructions, and issue templates for actionable user feedback.

## Upstream evidence

- [SlowAPI API](https://slowapi.readthedocs.io/en/latest/api/) and
  [source](https://github.com/laurentS/slowapi/blob/master/slowapi/extension.py): defaults,
  shared limits, local fallback, integer/callable costs. Source review matters because
  API documentation signatures can lag implementation.
- [limits async support](https://limits.readthedocs.io/en/stable/async.html): coredis or
  redis-py; [overview](https://limits.readthedocs.io/en/stable/) lists storage alternatives.
- [django-ratelimit security](https://django-ratelimit.readthedocs.io/en/stable/security.html):
  proxy normalization is explicitly left to deployment, not blind header trust.
- [FastAPI-Limiter README](https://github.com/long2ice/fastapi-limiter): current examples
  use PyrateLimiter, dependencies, custom identifiers/callbacks, and WebSocket limiting.

These links track upstream, so this is a dated review, not a permanent capability matrix.
No upstream code was copied or new algorithm adopted.

## Product work to prioritize next

1. **Repair compatibility contracts before promoting adapters.** SlowAPI `default_limits`
   and `headers_enabled` are accepted but not enforced; missing Request arguments skip
   limiting. Django callable keys return no identity and its adapter is not async-aware.
   Add explicit supported-option validation and regression tests before broad migration
   claims. Current documentation clearly identifies these limitations.
2. **One real application integration.** Measure application latency and rejection behavior
   under representative load, including outages and multiple workers. Publish a reproducible
   case study only after collecting data and obtaining the application's agreement.
3. **Hosted, built documentation.** The Mintlify configuration has not been deployment-tested.
   Validate/build a chosen hosting setup and use the verified URL for the repository homepage.
4. **Profile before adding algorithms.** Existing benchmark results favor limits in several
   workloads. Investigate digest construction, single-rule batch overhead, and async gates
   while preserving differential and atomicity checks. Avoid performance promises.
5. **Improve discoverability after the above.** Add accurate GitHub topics and homepage,
   publish a practical integration tutorial, and respond to reports. Track completed
   integrations and repeat usage; stars alone do not demonstrate adoption. No announcements
   or external metadata changes were made during this audit.

## Candidate applications in the owner's public portfolio

- [Tether](https://github.com/ArnavBallinCode/Tether): its `requirements.txt` pins
  FastAPI 0.115.*, and `tether/api/main.py` exposes dashboard/evaluation/recovery routes.
  A staging-only integration on an expensive operation is a plausible first case study.
  Confirm authentication and desired budget before selecting a route.
- [sentinel](https://github.com/ArnavBallinCode/sentinel): `services/api/pyproject.toml`
  requires Python >=3.11 and FastAPI >=0.115,<1, matching UniLimit's baseline. A candidate
  for authenticated API quotas; production traffic and endpoint costs were not audited.
- [crisis-dispatch-openenv](https://github.com/ArnavBallinCode/crisis-dispatch-openenv):
  pins FastAPI 0.115.0 but allows Python 3.10. UniLimit would require a Python 3.11/3.12
  deployment. Keep deterministic evaluation behavior separate from public API throttling.

These are candidates inferred from manifests and routes, not endorsements or integrations
already completed. No evidence was collected that these projects currently need rate limiting.
