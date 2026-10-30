"""The observability documentation is executable: every ```python block of each page runs,
in order, against a real Redis (the ``redis://localhost:6379/0`` URL is substituted)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "observability"
PAGES = sorted(p.name for p in DOCS.glob("*.md"))


def python_blocks(page: str) -> str:
    return "\n\n".join(re.findall(r"```python\n(.*?)```", (DOCS / page).read_text(), re.S))


def test_every_page_is_listed() -> None:
    assert PAGES == ["benchmarks.md", "events.md", "fallback.md", "logging.md", "metrics.md"]


@pytest.mark.parametrize("page", ["events.md", "fallback.md", "metrics.md"])
def test_examples_run_as_written(page: str, redis_url: str) -> None:
    code = python_blocks(page).replace("redis://localhost:6379/0", redis_url)
    assert code, page
    namespace: dict[str, Any] = {"__name__": f"docs_{page.removesuffix('.md')}"}
    exec(compile(code, str(DOCS / page), "exec"), namespace)
    for value in namespace.values():  # close what the page created
        close = getattr(value, "close", None)
        if type(value).__name__ == "Limiter" and callable(close):
            close()


def test_logging_page_example_is_valid() -> None:
    code = python_blocks("logging.md")
    namespace: dict[str, Any] = {}
    exec(compile(code, "logging.md", "exec"), namespace)  # configure_logging(interval=10)
    from unilimit._logging import DEFAULT_INTERVAL, throttled

    assert throttled.interval == 10
    throttled.configure(DEFAULT_INTERVAL)


def test_documented_metric_names_exist() -> None:
    from prometheus_client import CollectorRegistry, generate_latest

    from unilimit.integrations.prometheus import PrometheusObserver

    registry = CollectorRegistry()
    PrometheusObserver(registry)
    exposed = generate_latest(registry).decode()
    documented = set(re.findall(r"`(unilimit_[a-z_]+)`", (DOCS / "metrics.md").read_text()))
    for name in documented:
        assert name.removesuffix("_total") in exposed or name in exposed, name
