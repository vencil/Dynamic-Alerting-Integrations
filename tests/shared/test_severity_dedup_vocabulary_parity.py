"""`_severity_dedup` 的詞彙表：Python 產生器與 exporter 同一份（#2906）。

路由產生器（`_grar_parse.SEVERITY_DEDUP_ENABLE_WORDS` / `_DISABLE_WORDS`）
與 exporter 的 `ResolveSeverityDedup`（`pkg/config/resolve.go`）各自判讀
`_severity_dedup`：一個決定要不要產生 inhibit rule，一個決定要不要送出
sentinel alert。兩邊的詞不一樣，同一個值就會一邊開、一邊關。

做法：直接讀 Go 原始碼裡 `ResolveSeverityDedup` 的兩個 `case` 行（不另放
testdata）。找不到函式、找不到恰好兩個 `case`、或某個 `case` 不是純字串
清單，都算失敗——讀不到不能當成一致。schema 的 `definitions.severityDedup`
pattern 也一起對：它要恰好收下這些詞（不分大小寫），其他的都不收。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))

from _grar_parse import (  # noqa: E402
    SEVERITY_DEDUP_DISABLE_WORDS,
    SEVERITY_DEDUP_ENABLE_WORDS,
    severity_dedup_mode,
)

RESOLVE_GO = REPO / "components" / "threshold-exporter" / "app" / "pkg" / "config" / "resolve.go"
SCHEMA = REPO / "docs" / "schemas" / "tenant-config.schema.json"


def _go_cases() -> list[tuple[str, ...]]:
    src = RESOLVE_GO.read_text(encoding="utf-8")
    m = re.search(r"^func \(c \*ThresholdConfig\) ResolveSeverityDedup\(\)[^\n]*\{\n(.*?)^\}\n",
                  src, re.S | re.M)
    assert m, f"ResolveSeverityDedup not found in {RESOLVE_GO}"
    body = m.group(1)
    assert "strings.TrimSpace(strings.ToLower(" in body, (
        "ResolveSeverityDedup no longer trims and lowercases the value; "
        "severity_dedup_mode (strip + lower) must follow it")
    cases = re.findall(r"^\s*case ([^:\n]*):\s*$", body, re.M)
    assert len(cases) == 2, f"expected the enable and the disable case, found {cases!r}"
    out = []
    for c in cases:
        items = [x.strip() for x in c.split(",")]
        assert all(re.fullmatch(r'"[a-z]+"', x) for x in items), (
            f"a case that is not a list of string literals: {c!r}")
        out.append(tuple(x.strip('"') for x in items))
    return out


def test_python_words_are_the_exporters():
    enable, disable = _go_cases()
    assert set(SEVERITY_DEDUP_ENABLE_WORDS) == set(enable)
    assert set(SEVERITY_DEDUP_DISABLE_WORDS) == set(disable)


def test_python_reads_each_word_as_the_exporter_does():
    enable, disable = _go_cases()
    for w in enable:
        for spelling in (w, w.upper(), f"  {w.capitalize()} "):
            assert severity_dedup_mode(spelling) == "enable", spelling
    for w in disable:
        for spelling in (w, w.upper(), f"  {w.capitalize()} "):
            assert severity_dedup_mode(spelling) == "disable", spelling
    # Not a word of either case: refused (the exporter logs and enables).
    for other in ("no", "yes", "", "enabl", "0"):
        assert severity_dedup_mode(other) is None, other


def test_schema_takes_exactly_the_words():
    d = json.loads(SCHEMA.read_text(encoding="utf-8"))["definitions"]["severityDedup"]
    pattern = next(b["pattern"] for b in d["anyOf"] if b.get("type") == "string")
    enable, disable = _go_cases()
    for w in enable + disable:
        for spelling in (w, w.upper(), w.capitalize(), f" {w} "):
            assert re.search(pattern, spelling), spelling
    for other in ("no", "yes", "", "enabl", "disables", "0", "y", "n"):
        assert not re.search(pattern, other), other
    assert {"type": ["boolean", "null"]} in d["anyOf"]
