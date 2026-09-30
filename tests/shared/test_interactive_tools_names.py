"""test_interactive_tools_names.py — the interactive-tools overview names tools as the registry does

``docs/interactive-tools{,.en}.md`` hand-picks a few tools into its overview
table. The names drifted from ``docs/assets/tool-registry.yaml`` (the SSOT the
portal renders), so a reader looked for a "Tenant YAML Playground" the portal
calls "Tenant YAML Syntax Checker". Each bold name in the table must be the
registry title in that file's language.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REGISTRY = REPO_ROOT / "docs/assets/tool-registry.yaml"
_ROW = re.compile(r"^\|\s*\*\*(.+?)\*\*\s*\|")


def _titles(lang: str) -> set[str]:
    reg = yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))
    tools = reg["tools"] if isinstance(reg, dict) else reg
    return {t["title"][lang] for t in tools}


def _table_names(path: Path) -> list[str]:
    return [m.group(1) for line in path.read_text(encoding="utf-8").splitlines()
            if (m := _ROW.match(line))]


@pytest.mark.parametrize("doc, lang", [
    ("docs/interactive-tools.md", "zh"),
    ("docs/interactive-tools.en.md", "en"),
])
def test_overview_names_are_registry_titles(doc: str, lang: str) -> None:
    names = _table_names(REPO_ROOT / doc)
    assert names, f"{doc}: no bold tool names found — did the table format change?"
    unknown = [n for n in names if n not in _titles(lang)]
    assert not unknown, (
        f"{doc}: {unknown} are not `title.{lang}` of any tool in "
        f"docs/assets/tool-registry.yaml — use the registry title")
