"""WebSocket limits and limits-style multi-rate strings, over real ASGI."""

from __future__ import annotations

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect

from unilimit import Decision, Identity, Rule
from unilimit.integrations.fastapi import RateLimit, WebSocketRateLimit, setup

from .conftest import LimiterFactory, WebSocketSession, client, ratelimit_headers


async def test_multi_rate_string_is_atomic(make_limiter: LimiterFactory) -> None:
    """slowapi-style "2/minute;3/day" becomes two rules consumed all-or-nothing."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    @app.get("/x", dependencies=[Depends(RateLimit("x", rate="2/minute;3/day"))])
    async def x() -> dict[str, bool]:
        return {"ok": True}

    async with client(app) as c:
        responses = [await c.get("/x") for _ in range(4)]
    assert [r.status_code for r in responses] == [200, 200, 429, 429]
    assert ratelimit_headers(responses[1])["x-ratelimit-limit"] == "2"  # minute binds
    ip = Identity("ip", "203.0.113.10")
    day = Rule("x:3/1d", "3/day")
    assert limiter.peek(day, ip).remaining == 1  # rejected requests consumed nothing


async def test_websocket_handshake_limit(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())

    @app.websocket("/ws", dependencies=[Depends(WebSocketRateLimit("ws-connect", rate="2/minute"))])
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    answers = []
    for _ in range(3):
        session = WebSocketSession(app, "/ws")
        answers.append(await session.connect())
        await session.task
    assert [a["type"] for a in answers] == [
        "websocket.accept",
        "websocket.accept",
        "websocket.close",
    ]
    assert answers[2]["code"] == 1008  # policy violation, before the handshake completed


async def test_websocket_per_message_limit(make_limiter: LimiterFactory) -> None:
    app = FastAPI()
    setup(app, make_limiter())
    per_message = WebSocketRateLimit("ws-message", rate="3/minute", algorithm="fixed_window")

    @app.websocket("/chat")
    async def chat(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                text = await websocket.receive_text()
                decision: Decision = await per_message.consume(websocket)
                if decision.allowed:
                    await websocket.send_text(f"echo:{text}")
                else:
                    await websocket.send_text(f"limited:{decision.retry_after is not None}")
        except WebSocketDisconnect:
            pass

    session = WebSocketSession(app, "/chat")
    assert (await session.connect())["type"] == "websocket.accept"
    replies = []
    for i in range(5):
        await session.send_text(str(i))
        replies.append(await session.receive_text())
    await session.close()
    assert replies == ["echo:0", "echo:1", "echo:2", "limited:True", "limited:True"]


async def test_websocket_identity_dependency(make_limiter: LimiterFactory) -> None:
    def user_from_path(websocket: WebSocket) -> str:
        return str(websocket.path_params["user"])

    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)
    connect = WebSocketRateLimit("ws-user", rate="1/minute", identity=user_from_path)

    @app.websocket("/u/{user}", dependencies=[Depends(connect)])
    async def ws(websocket: WebSocket, user: str) -> None:
        await websocket.accept()
        await websocket.close()

    results = []
    for path in ("/u/alice", "/u/alice", "/u/bob"):
        session = WebSocketSession(app, path)
        results.append((await session.connect())["type"])
        await session.task
    assert results == ["websocket.accept", "websocket.close", "websocket.accept"]
    assert (
        limiter.peek(Rule("ws-user", "1/minute", key="user"), Identity("user", "bob")).remaining
        == 0
    )
