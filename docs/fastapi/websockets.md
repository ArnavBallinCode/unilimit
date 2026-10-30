# WebSockets

WebSocket connections do not pass through the HTTP middleware, so global rules do not
apply to them. UniLimit provides two dedicated, complementary tools for WebSockets:

1. **Connection concurrency (`WebSocketConnectionLimit`):** limits simultaneous open
   connections across workers and nodes (e.g. at most 5 concurrent connections per user
   or per IP), with automated heartbeat renewal, configurable lease TTLs, and crash
   recovery.
2. **Rate limits (`WebSocketRateLimit`):** limits the rate of connection handshakes and
   individual messages, with support for multi-rate rules and weighted message budgets.

---

### Example: Connection and Message Rate Limiting

```python
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect

from unilimit import Limiter
from unilimit.integrations.fastapi import (
    WebSocketConnectionLimit,
    WebSocketRateLimit,
    setup,
)

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

---

### Distributed Concurrent Connection Limits

To cap the number of simultaneous active connections per user or IP across all workers
in your cluster, use `WebSocketConnectionLimit`:

```python
concurrency = WebSocketConnectionLimit(
    "active-conns",
    max_concurrent=3,
    lease_ttl=30.0,  # 30-second lease
    heartbeat_interval=15.0,  # Renewed every 15 seconds in the background
    close_code=1008,  # WS_1008_POLICY_VIOLATION
    close_reason="maximum concurrent connections exceeded",
)


@app.websocket("/stream", dependencies=[Depends(concurrency)])
async def stream(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
```

You can also manage leases directly inside a handler with an async context manager:

```python
@app.websocket("/custom-stream")
async def custom_stream(websocket: WebSocket) -> None:
    async with concurrency.manage(websocket) as lease:
        await websocket.accept()
        # lease.current describes the active count for this identity
        while True:
            await websocket.receive_text()
```

- **Crash Recovery:** If a server worker crashes or loses network connectivity, its
  leases automatically expire in Redis after `lease_ttl` seconds, freeing concurrency
  slots without requiring manual intervention.
- **Heartbeats:** As long as the WebSocket connection remains open, a lightweight
  background task automatically renews the lease every `heartbeat_interval` seconds.

---

### Weighted Message Budgets

For messages with variable complexity or payload sizes (e.g. streaming audio, image
frames, or large JSON payloads), pass `cost` to `consume`:

```python
async def handle_payload(websocket: WebSocket, payload: bytes) -> None:
    # Charge 1 token base plus 1 token per kilobyte of payload
    cost = max(1, len(payload) // 1024)
    decision = await messages.consume(websocket, cost=cost)
```

---

### Configurable Message Rejection Policies

Use `gate()` or helper shortcuts to handle rejections cleanly:

```python
async def check_policies(websocket: WebSocket) -> None:
    # 1. Reply to client with JSON error and continue listening
    admitted = await messages.consume_or_reply(websocket, payload={"error": "rate_limited"})

    # 2. Quietly drop the frame without replying
    admitted = await messages.consume_or_drop(websocket)

    # 3. Close the socket on excess messages
    admitted = await messages.consume_or_close(websocket, code=1008)
```
