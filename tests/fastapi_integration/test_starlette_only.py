"""The Starlette integration works with ``unilimit[starlette]`` alone, without FastAPI."""

from __future__ import annotations

import subprocess
import sys
import textwrap

APP = textwrap.dedent(
    """
    import sys

    sys.modules["fastapi"] = None  # any `import fastapi` now fails, as if not installed

    import asyncio
    import json

    import httpx
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route, WebSocketRoute

    from unilimit import Limiter, MemoryBackend, Rule
    from unilimit.integrations.starlette import (
        UniLimitMiddleware,
        WebSocketConnectionLimit,
        WebSocketRateLimit,
    )

    limiter = Limiter(MemoryBackend(), global_rules=[Rule("global", "2/minute")])
    messages = WebSocketRateLimit("messages", rate="1/minute", limiter=limiter)
    connections = WebSocketConnectionLimit("conns", max_concurrent=1, limiter=limiter)


    async def home(request):
        return PlainTextResponse("ok")


    async def ws(websocket):
        async with connections.manage(websocket):
            await websocket.accept()
            allowed = [(await messages.consume(websocket)).allowed for _ in range(2)]
            await websocket.send_json(allowed)
            await websocket.close()


    app = Starlette(
        routes=[Route("/", home), WebSocketRoute("/ws", ws)],
        middleware=[Middleware(UniLimitMiddleware, limiter=limiter)],
    )


    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            assert [(await client.get("/")).status_code for _ in range(3)] == [200, 200, 429]
        to_app, from_app = asyncio.Queue(), asyncio.Queue()
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/ws",
            "raw_path": b"/ws", "query_string": b"", "headers": [], "subprotocols": [],
            "client": ("203.0.113.10", 5000), "server": ("t", 80), "state": {},
        }
        task = asyncio.create_task(app(scope, to_app.get, from_app.put))
        await to_app.put({"type": "websocket.connect"})
        assert (await from_app.get())["type"] == "websocket.accept"
        assert json.loads((await from_app.get())["text"]) == [True, False]
        assert (await from_app.get())["type"] == "websocket.close"
        await task


    asyncio.run(main())

    try:
        WebSocketRateLimit("x", rate="1/minute", identity=lambda: "u")
    except Exception as exc:
        assert "FastAPI" in str(exc), exc
    else:
        raise SystemExit("identity= without FastAPI should fail clearly")
    assert sys.modules["fastapi"] is None
    """
)


def test_starlette_integration_does_not_need_fastapi() -> None:
    subprocess.run([sys.executable, "-c", APP], check=True)
