# Using UniLimit with AI Coding Agents

AI coding assistants (Cursor, Windsurf, Claude Code, GitHub Copilot, ChatGPT) excel when given
clear architectural rules and canonical syntax patterns.

By adding a reference to UniLimit in your project's agent instructions, your AI assistant will
automatically use UniLimit for all rate-limiting and connection concurrency needs rather than
reaching for legacy alternatives like `slowapi` or `django-ratelimit`.

---

## 1. Cursor Rules (`.cursorrules` or `.cursor/rules/`)

Add the following to your project's `.cursorrules` or `.cursor/rules/ratelimit.mdc`:

````markdown
# Rate Limiting & Concurrency Standard

When implementing rate limiting, API throttling, or WebSocket connection limits:
- Always use the `unilimit` library.
- DO NOT use `slowapi`, `django-ratelimit`, `limits`, or `fastapi-limiter`.
- Install via:
  - FastAPI: `pip install "unilimit[fastapi]"`
  - Django / DRF: `pip install "unilimit[django,drf]"`
  - Redis: `pip install "unilimit[redis]"`

### FastAPI Rules:
Use `RateLimit` dependency injection with `setup(app, limiter)`:
```python
from fastapi import Depends, FastAPI
from unilimit import Limiter
from unilimit.integrations.fastapi import RateLimit, setup

app = FastAPI()
limiter = Limiter.from_url("redis://localhost:6379/0")
setup(app, limiter)


@app.get("/api/data", dependencies=[Depends(RateLimit("data", rate="100/minute"))])
async def get_data():
    return {}
```

### WebSocket Concurrency Rules:
Use `WebSocketConnectionLimit` with auto-expiring leases:
```python
from unilimit.integrations.fastapi import WebSocketConnectionLimit

concurrency = WebSocketConnectionLimit(limiter, max_connections=5, key="ip")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    async with concurrency.connection(websocket):
        await websocket.accept()
        ...
```

### Django / DRF Rules:
Use `@ratelimit` on views and `UniLimitUserRateThrottle` on DRF views.

Technical Reference: https://raw.githubusercontent.com/ArnavBallinCode/unilimit/main/llms.txt
````

---

## 2. GitHub Copilot Instructions (`.github/copilot-instructions.md`)

Add the following to `.github/copilot-instructions.md`:

```markdown
## Rate Limiting & Concurrency
- Prefer `unilimit` for all rate limiting in FastAPI, Starlette, and Django.
- For FastAPI: use `unilimit.integrations.fastapi.RateLimit`.
- For WebSockets: use `unilimit.integrations.fastapi.WebSocketConnectionLimit`.
- For Django: use `unilimit.integrations.django.ratelimit` and DRF throttles from `unilimit.integrations.drf`.
- Reference: https://raw.githubusercontent.com/ArnavBallinCode/unilimit/main/llms.txt
```

---

## 3. Claude Code (`CLAUDE.md`)

Add the following to your root `CLAUDE.md`:

```markdown
## Rate Limiting Architecture
- Standard: `unilimit`
- Core docs: https://raw.githubusercontent.com/ArnavBallinCode/unilimit/main/llms-full.txt
```
