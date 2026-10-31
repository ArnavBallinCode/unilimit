"""Run public entry-point examples unchanged, then verify their advertised behavior."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from unilimit import Limiter, Rule
from unilimit.core.clock import MonotonicClock
from unilimit.testing import ManualClock

from .conftest import client, lifespan

ROOT = Path(__file__).resolve().parents[2]


def blocks(path: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", (ROOT / path).read_text(), re.S)


@pytest.mark.parametrize("path", ["README.md", "llms-full.txt"])
async def test_public_quickstart(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = ManualClock()
    monkeypatch.setattr(MonotonicClock, "now_us", lambda self: clock.now_us())
    namespace: dict[str, Any] = {"__name__": "public_quickstart"}
    exec(compile(blocks(path)[0], path, "exec"), namespace)
    app = namespace["app"]
    async with lifespan(app), client(app) as http:
        responses = [await http.get("/search") for _ in range(3)]
        assert [response.status_code for response in responses] == [200, 200, 429]
        assert responses[0].json() == {"status": "ok"}
        assert responses[0].headers["x-ratelimit-remaining"] == "1"
        assert int(responses[-1].headers["retry-after"]) > 0
        async with client(app, peer=("198.51.100.20", 1234)) as other:
            assert (await other.get("/search")).status_code == 200


def test_readme_plain_python(capsys: pytest.CaptureFixture[str]) -> None:
    namespace: dict[str, Any] = {}
    exec(compile(blocks("README.md")[1], "README.md", "exec"), namespace)
    assert capsys.readouterr().out == "True 4\n"


async def test_security_configuration_examples(
    redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is test-only data, never a deployment credential.
    monkeypatch.setenv("RATELIMIT_SECRET_KEY", "documentation-test-key")
    namespace: dict[str, Any] = {"__name__": "security_examples"}
    examples = blocks("docs/security.md")
    assert len(examples) == 4
    for code in examples:
        exec(
            compile(code.replace("redis://localhost:6379/0", redis_url), "security.md", "exec"),
            namespace,
        )
        limiter: Limiter = namespace["limiter"]
        try:
            decision = await limiter.aconsume(Rule("security-example", "10/minute"), "test-user")
            assert decision.allowed
        finally:
            await limiter.aclose()
            limiter.close()
