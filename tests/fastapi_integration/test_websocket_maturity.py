"""Comprehensive integration tests for WebSocket connection concurrency and message maturity."""

from __future__ import annotations

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect

from unilimit.integrations.fastapi import (
    WebSocketConnectionLimit,
    WebSocketRateLimit,
    setup,
)

from .conftest import LimiterFactory, WebSocketSession


async def test_websocket_connection_concurrency_dependency(
    make_limiter: LimiterFactory,
) -> None:
    """Tests that WebSocketConnectionLimit strictly bounds concurrent connections."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    conn_limit = WebSocketConnectionLimit("ws-cap", max_concurrent=2, lease_ttl=5.0)

    @app.websocket("/ws", dependencies=[Depends(conn_limit)])
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                await websocket.send_text(f"ack:{data}")
        except WebSocketDisconnect:
            pass

    # Open connection 1
    session1 = WebSocketSession(app, "/ws")
    ans1 = await session1.connect()
    assert ans1["type"] == "websocket.accept"

    # Open connection 2
    session2 = WebSocketSession(app, "/ws")
    ans2 = await session2.connect()
    assert ans2["type"] == "websocket.accept"

    # Connection 3 should be rejected with 1008 before accept
    session3 = WebSocketSession(app, "/ws")
    ans3 = await session3.connect()
    assert ans3["type"] == "websocket.close"
    assert ans3["code"] == 1008
    await session3.task

    # Now close connection 1
    await session1.close()
    await session1.task

    # Connection 3 can now connect successfully!
    session3_retry = WebSocketSession(app, "/ws")
    ans3_retry = await session3_retry.connect()
    assert ans3_retry["type"] == "websocket.accept"

    # Clean up
    await session2.close()
    await session2.task
    await session3_retry.close()
    await session3_retry.task


async def test_websocket_connection_concurrency_context_manager(
    make_limiter: LimiterFactory,
) -> None:
    """Tests using WebSocketConnectionLimit directly with async with manage()."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    conn_limit = WebSocketConnectionLimit("ws-mgr", max_concurrent=1, lease_ttl=5.0)

    @app.websocket("/stream")
    async def stream(websocket: WebSocket) -> None:
        async with conn_limit.manage(websocket):
            await websocket.accept()
            try:
                while True:
                    _ = await websocket.receive_text()
            except WebSocketDisconnect:
                pass

    session1 = WebSocketSession(app, "/stream")
    assert (await session1.connect())["type"] == "websocket.accept"

    # 2nd is rejected
    session2 = WebSocketSession(app, "/stream")
    assert (await session2.connect())["type"] == "websocket.close"
    await session2.task

    await session1.close()
    await session1.task


async def test_websocket_per_user_concurrency(make_limiter: LimiterFactory) -> None:
    """Verifies that connection concurrency caps are enforced per user identity."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    def get_user(websocket: WebSocket) -> str:
        return str(websocket.query_params.get("user", "anon"))

    conn_limit = WebSocketConnectionLimit(
        "user-conns", max_concurrent=1, key="user", identity=get_user
    )

    @app.websocket("/user-ws", dependencies=[Depends(conn_limit)])
    async def user_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    # Alice connects
    alice1 = WebSocketSession(app, "/user-ws?user=alice")
    assert (await alice1.connect())["type"] == "websocket.accept"

    # Bob connects (allowed: separate user identity)
    bob1 = WebSocketSession(app, "/user-ws?user=bob")
    assert (await bob1.connect())["type"] == "websocket.accept"

    # Alice second connection rejected
    alice2 = WebSocketSession(app, "/user-ws?user=alice")
    assert (await alice2.connect())["type"] == "websocket.close"
    await alice2.task

    await alice1.close()
    await alice1.task
    await bob1.close()
    await bob1.task


async def test_websocket_weighted_message_budgets(make_limiter: LimiterFactory) -> None:
    """Verifies weighted message budgets where cost scales with message length or complexity."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    msg_limit = WebSocketRateLimit("ws-budget", rate="10/minute", algorithm="token_bucket")

    @app.websocket("/weighted")
    async def weighted_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                msg = await websocket.receive_text()
                # Cost scales: 1 token per 5 chars (min 1)
                cost = max(1, len(msg) // 5)
                decision = await msg_limit.consume(websocket, cost=cost)
                if decision.allowed:
                    await websocket.send_text(f"ok:{decision.remaining}")
                else:
                    await websocket.send_text("limited")
        except WebSocketDisconnect:
            pass

    session = WebSocketSession(app, "/weighted")
    assert (await session.connect())["type"] == "websocket.accept"

    # Send message of length 30 (cost = 6) -> remaining = 4
    await session.send_text("123456789012345678901234567890")
    reply1 = await session.receive_text()
    assert reply1.startswith("ok:")

    # Send message of length 30 again (cost = 6, but only 4 remain) -> rejected
    await session.send_text("123456789012345678901234567890")
    reply2 = await session.receive_text()
    assert reply2 == "limited"

    # Send small message (cost = 1) -> admitted! (tokens remained)
    await session.send_text("hi")
    reply3 = await session.receive_text()
    assert reply3.startswith("ok:")

    await session.close()
    await session.task


async def test_websocket_message_rejection_policies(make_limiter: LimiterFactory) -> None:
    """Tests gate() policies: reply, drop, close, and raise."""
    limiter = make_limiter()
    app = FastAPI()
    setup(app, limiter)

    msg_limit = WebSocketRateLimit("ws-msg-rate", rate="2/minute", algorithm="fixed_window")

    @app.websocket("/policies")
    async def policies_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                msg = await websocket.receive_text()
                action = msg.split(":")[0]
                if action == "reply":
                    admitted = await msg_limit.consume_or_reply(
                        websocket, {"error": "slow_down"}, cost=1
                    )
                    if admitted:
                        await websocket.send_text("processed")
                elif action == "drop":
                    admitted = await msg_limit.consume_or_drop(websocket, cost=1)
                    if admitted:
                        await websocket.send_text("processed")
                elif action == "close":
                    admitted = await msg_limit.consume_or_close(websocket, code=1008, cost=1)
                    if admitted:
                        await websocket.send_text("processed")
        except WebSocketDisconnect:
            pass

    # Test "reply" policy
    session = WebSocketSession(app, "/policies")
    assert (await session.connect())["type"] == "websocket.accept"

    # 1st: processed
    await session.send_text("reply:msg1")
    assert await session.receive_text() == "processed"

    # 2nd: processed
    await session.send_text("reply:msg2")
    assert await session.receive_text() == "processed"

    # 3rd: rate-limited, answers with error JSON payload
    await session.send_text("reply:msg3")
    ans = await session.from_app.get()
    assert "slow_down" in ans["text"]

    await session.close()
    await session.task
