"""Keeps the agent-facing documentation (AGENTS.md, CLAUDE.md, context/) accurate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT_DOCS = [
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "README.md",
    ROOT / ".github" / "copilot-instructions.md",
    *sorted((ROOT / "context").glob("*.md")),
]
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


@pytest.mark.parametrize("doc", AGENT_DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc: Path) -> None:
    for target in LINK.findall(doc.read_text(encoding="utf-8")):
        if "://" in target:
            continue
        assert (doc.parent / target).resolve().exists(), f"{doc.name}: broken link {target}"


def test_tests_cited_in_context_exist() -> None:
    defined = {
        m.group(1)
        for path in (ROOT / "tests").rglob("test_*.py")
        for m in re.finditer(r"def (test_\w+)", path.read_text(encoding="utf-8"))
    }
    cited = {
        name
        for doc in (ROOT / "context").glob("*.md")
        for name in re.findall(r"`(test_\w+)`", doc.read_text(encoding="utf-8"))
    }
    assert cited, "context/ should cite the tests that enforce invariants"
    assert cited <= defined, f"cited tests that do not exist: {sorted(cited - defined)}"


def test_claude_md_imports_agents_md() -> None:
    assert "@AGENTS.md" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
