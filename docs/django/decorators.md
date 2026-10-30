# View decorators

`@ratelimit` limits one view.

```python
from django.http import HttpRequest, JsonResponse
from django.urls import path
from django.utils.decorators import method_decorator
from django.views import View

from unilimit import Limiter, Rule
from unilimit.integrations.django import ratelimit

limiter = Limiter.from_url(
    "redis://localhost:6379/0",
    # Rules registered on the limiter can be used by name.
    rules=[Rule("free-tier", "100/hour"), Rule("burst", "5/second")],
)
UNILIMIT = {"LIMITER": limiter}


@ratelimit(rate="10/minute")  # inline; the rule is named after the view
def search(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"results": []})


@ratelimit("login", rate="5/minute", key="ip", methods=["POST"])  # POST only
def login(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("daily", rate="2/minute")
def daily(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"report": "daily"})


@ratelimit("daily", rate="2/minute")  # same name, same counter
def weekly(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"report": "weekly"})


@ratelimit("api", rate="10/second;1000/day")  # several rates, one atomic batch
def api(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit("free-tier", "burst")  # several named rules, consumed together
def tiered(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@ratelimit(Rule("export", "2/minute", key="ip"), Rule("export-all", "50/minute", key="global"))
def export(request: HttpRequest) -> JsonResponse:  # a per-client limit plus one ceiling
    return JsonResponse({"ok": True})


@ratelimit("heavy", rate="20/minute", cost=5)  # each request counts for 5
async def heavy(request: HttpRequest) -> JsonResponse:  # async views are fine
    return JsonResponse({"ok": True})


@method_decorator(ratelimit("articles", rate="3/minute"), name="dispatch")
class Articles(View):  # class-based views: decorate dispatch
    def get(self, request: HttpRequest) -> JsonResponse:
        return JsonResponse({"articles": []})


urlpatterns = [
    path("search", search),
    path("login", login),
    path("daily", daily),
    path("weekly", weekly),
    path("api", api),
    path("tiered", tiered),
    path("export", export),
    path("heavy", heavy),
    path("articles", Articles.as_view()),
]
```

## Rules

| Form | Meaning |
|---|---|
| `@ratelimit(rate="10/minute")` | Inline rule named after the view. |
| `@ratelimit("name", rate=...)` | Inline rule with a name. Views using the same name and key share a counter. |
| `@ratelimit("a", "b")` | Rules registered on the `Limiter` (`rules=[...]`), by name. |
| `@ratelimit(Rule(...), Rule(...))` | `Rule` objects. |

All rules of one decorator are consumed in one atomic batch. A request rejected by any of
them consumes nothing from the others. Stacked decorators are separate batches, outermost
first.

Inline rules key on `"ip"` unless you pass `key=`; see
[authentication and identities](authentication.md). Other options: `algorithm`
(`"sliding_window"` by default, `"fixed_window"`, `"token_bucket"` with `burst=`),
`cost`, `methods`, `when`, `on_missing`, `fail_mode` and `action="log"` (count and
log but never reject).

Invalid arguments and unknown rule names raise `ConfigurationError` when the module is
imported, not on the request path.

## Class-based views

`@ratelimit` decorates functions. For class-based views use `method_decorator` on
`dispatch`, as shown above. Both sync and async class-based views are supported.

## Streaming responses

Limiting happens before the view runs. Headers are added to the response object, so
`StreamingHttpResponse` carries them like any other response.
