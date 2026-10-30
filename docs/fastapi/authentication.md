# Limiting authenticated users

Pass your authentication dependency as `identity=`. FastAPI resolves it **before** the
limit is evaluated and caches it for the request, so an endpoint that also depends on
it does not call it twice. Its return value, a user id (`str` or `int`) or `None`,
becomes the `"user"` identity.

```python
from fastapi import Depends, FastAPI, Header, HTTPException

from unilimit import Limiter
from unilimit.integrations.fastapi import RateLimit, setup

USERS_BY_TOKEN = {"token-alice": 1, "token-bob": 2}


async def current_user_id(authorization: str = Header()) -> int:
    user_id = USERS_BY_TOKEN.get(authorization.removeprefix("Bearer "))
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user_id


limiter = Limiter.from_url("redis://localhost:6379/0")
app = FastAPI()
setup(app, limiter)


@app.get("/profile")
async def profile(
    _: object = Depends(RateLimit("profile", rate="30/minute", identity=current_user_id)),
    user_id: int = Depends(current_user_id),
) -> dict[str, int]:
    return {"user_id": user_id}
```

Requests with an invalid token are rejected by `current_user_id` (401) before any quota
is consumed. Each user has their own counter.

## Anonymous visitors and fallback chains

An identity dependency may return `None` for anonymous visitors. What happens then is
decided by `on_missing`:

| `on_missing` | Anonymous request |
|---|---|
| `"deny"` (default) | Rejected with 403. Rate limiting is never bypassed silently. |
| `"raise"` | `IdentityError`, a server error: use it when anonymity is a bug. |
| `"skip"` | The rule does not apply. |

Usually you want anonymous visitors limited by IP instead. That is a **fallback chain**:

```python
async def optional_user_id(authorization: str | None = Header(None)) -> int | None:
    if authorization is None:
        return None
    return USERS_BY_TOKEN.get(authorization.removeprefix("Bearer "))


# Authenticated: per user. Anonymous: per trusted client IP. Separate counters.
browse = RateLimit("browse", rate="60/minute", identity=optional_user_id, key="user_or_ip")


@app.get("/articles", dependencies=[Depends(browse)])
async def articles() -> list[str]:
    return []
```

## Different limits per plan

Use one rule per plan, with `when=`. Only the rule whose condition matches applies:

```python
from fastapi import Request

from unilimit import Rule

PLANS = {1: "pro", 2: "free"}


async def user_and_plan(request: Request, user_id: int = Depends(current_user_id)) -> int:
    request.state.plan = PLANS.get(user_id, "free")
    return user_id


tiers = RateLimit(
    Rule("free", "10/minute", key="user", when=lambda ctx: ctx.raw.state.plan == "free"),
    Rule("pro", "1000/minute", key="user", when=lambda ctx: ctx.raw.state.plan == "pro"),
    identity=user_and_plan,
)


@app.get("/api/data", dependencies=[Depends(tiers)])
async def data() -> dict[str, bool]:
    return {"ok": True}
```

`ctx.raw` is the Starlette `Request`, so conditions and custom identity functions can
read anything your dependencies stored on `request.state`.
