# WebSockets

WebSocket connections do not pass through the HTTP middleware, so global rules do not
apply to them. Use `WebSocketRateLimit`, which takes the same arguments as `RateLimit`:
inline rules, rule names, `Rule` objects, `identity=` and `key=`.

- **Connection attempts:** add it as a dependency of the WebSocket route. A rejected
  handshake is closed with code **1008** (policy violation) before it is accepted.
- **Messages:** call `await limit.consume(websocket)` for each message. It returns the
  `Decision` and never raises, so your handler chooses what a rejected message means.

```python
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect

from unilimit import Limiter
from unilimit.integrations.fastapi import WebSocketRateLimit, setup

limiter = Limiter.from_url("redis://localhost:6379/0")
app = FastAPI()
setup(app, limiter)

connections = WebSocketRateLimit("chat-connect", rate="10/minute")
messages = WebSocketRateLimit("chat-message", rate="5/second;100/minute")


@app.websocket("/chat", dependencies=[Depends(connections)])
async def chat(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            text = await websocket.receive_text()
            decision = await messages.consume(websocket)
            if not decision.allowed:
                await websocket.send_json(
                    {"error": "rate_limited", "retry_after": decision.retry_after}
                )
                continue
            await websocket.send_text(f"echo: {text}")
    except WebSocketDisconnect:
        pass
```

For per-user message limits, authenticate when the client connects and pass the user
id on every call: `await messages.consume(websocket, user_id=user_id)`. Message rules
then use `key="user"`.

Each message is its own atomic evaluation: with `"5/second;100/minute"`, a message is
admitted only if both counters have room, and a rejected message consumes neither.
