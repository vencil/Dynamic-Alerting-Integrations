"""`da-guard served-values` 的旗標表要列出程式宣告的每個旗標（中英兩份）。

旗標由 served_values.go 的 FlagSet 宣告；文件的表是讀者唯一看得到旗標的地方，
漏列就等於讀者不知道有這個旗標（`--disable-effects` 曾經漏列）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "components/threshold-exporter/app/cmd/da-guard/served_values.go"
_DECL = re.compile(r'fs\.\w+Var\(&f\.\w+,\s*"([a-z][a-z-]*)"')


def _declared() -> set[str]:
    names = set(_DECL.findall(SOURCE.read_text(encoding="utf-8")))
    assert names, f"no flag declaration found in {SOURCE}"
    return names


def _documented(doc: Path) -> set[str]:
    lines = doc.read_text(encoding="utf-8").splitlines()
    start = lines.index("**`served-values`**")
    rows: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    assert rows, f"no flag table after **`served-values`** in {doc.name}"
    return {m.group(1) for r in rows if (m := re.match(r"\| `--([a-z][a-z-]*)", r))}


@pytest.mark.parametrize("doc", ["docs/cli-reference.md", "docs/cli-reference.en.md"])
def test_every_declared_flag_is_in_the_table(doc):
    assert _declared() - _documented(REPO / doc) == set()
