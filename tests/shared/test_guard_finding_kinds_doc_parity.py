"""cli-reference 的 da-guard finding kind 表格與 Go 登錄表雙向一致（#4076）。

`docs/cli-reference.md`（與 `.en.md`）在 `unregistered_finding_kind` 那段說每筆 finding 的
`kind` 都來自下方各表這份封閉清單；清單本身在 Go：`internal/guard/findingkind/kinds.go`
的 `register("…")`（`findingkind.All()`）。表格是手寫的（「觸發」欄比 Go 的 reason 詳細得
多，不生成），這支測試讓兩邊漂移在合入前就紅：

* 「Routing 檢查」以外的表：第一欄的 kind 恰好等於 Go 登錄的 kind，各出現一次。
* 「Routing 檢查」那張：da-guard 已不判路由（ADR-036 §7 步驟 3），表格保留作為 route
  generator 同名 finding 的定義，所以每個 kind 都要是 `_grar_merge.FINDING_KINDS` 的一員，
  而且不在 Go 登錄表裡。

讀 Go 原始碼與 markdown 而不是另放清單：找不到章節、表格，或一個 kind 都沒讀到，都算失
敗——讀不到不能當成一致（比照 `test_key_verdict_kinds_parity.py`）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))

from _grar_merge import FINDING_KINDS  # noqa: E402

KINDS_GO = (REPO / "components" / "threshold-exporter" / "app" / "internal" / "guard"
            / "findingkind" / "kinds.go")
#: doc → the bold line that opens the routing section in its `#### guard`.
DOCS = {
    REPO / "docs" / "cli-reference.md": "**Routing 檢查（",
    REPO / "docs" / "cli-reference.en.md": "**Routing checks (",
}


def _go_kinds() -> list[str]:
    src = KINDS_GO.read_text(encoding="utf-8")
    kinds = re.findall(r'\bregister\(\s*"([a-z_]+)"', src)
    assert kinds, f"no register(\"…\") read from {KINDS_GO}"
    # 每一個 register( 呼叫都要被讀到：pattern 漏掉的就是這支測試悄悄沒比的一項。
    assert len(kinds) == len(re.findall(r"\bregister\(", src)), "a register( call the pattern missed"
    return kinds


def _tables(doc: Path, routing_marker: str) -> tuple[list[str], list[str]]:
    """(routing table kinds, every other table's kinds) of *doc*'s `#### guard`."""
    text = doc.read_text(encoding="utf-8")
    start = text.find("\n#### guard\n")
    assert start >= 0, f"no `#### guard` section in {doc}"
    end = text.find("\n#### ", start + 1)
    section = text[start:end if end >= 0 else len(text)]
    marker = section.find(routing_marker)
    assert marker >= 0, f"no {routing_marker!r} in {doc}'s `#### guard`"
    routing, other = [], []
    lines = section.splitlines(keepends=True)
    offset = 0
    in_table = False
    first_after_marker = None
    tables = 0
    for line in lines:
        pos, offset = offset, offset + len(line)
        if line.startswith("| Finding kind |"):
            in_table, tables = True, tables + 1
            if first_after_marker is None and pos > marker:
                first_after_marker = tables
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        if not in_table or re.match(r"^\|\s*-", line):
            continue
        m = re.match(r"^\| `([a-z_]+)` \|", line)
        assert m, f"{doc}: a finding-kind row whose first cell is not one `kind`: {line[:80]!r}"
        (routing if tables == first_after_marker else other).append(m.group(1))
    assert tables and routing and other, f"{doc}: finding-kind tables not read ({tables} tables)"
    return routing, other


@pytest.mark.parametrize("doc", sorted(DOCS), ids=lambda p: p.name)
def test_the_tables_list_exactly_the_registered_kinds(doc):
    _, other = _tables(doc, DOCS[doc])
    dupes = sorted({k for k in other if other.count(k) > 1})
    assert not dupes, f"{doc.name}: listed twice: {dupes}"
    go = set(_go_kinds())
    assert not go - set(other), f"{doc.name}: registered in Go, no table row: {sorted(go - set(other))}"
    assert not set(other) - go, f"{doc.name}: a table row Go does not register: {sorted(set(other) - go)}"


@pytest.mark.parametrize("doc", sorted(DOCS), ids=lambda p: p.name)
def test_the_routing_table_names_route_generator_kinds(doc):
    routing, _ = _tables(doc, DOCS[doc])
    assert not set(routing) - set(FINDING_KINDS), sorted(set(routing) - set(FINDING_KINDS))
    assert not set(routing) & set(_go_kinds()), sorted(set(routing) & set(_go_kinds()))


def test_both_languages_list_the_same_kinds():
    a, b = (_tables(d, DOCS[d]) for d in sorted(DOCS))
    assert sorted(a[0]) == sorted(b[0]) and sorted(a[1]) == sorted(b[1])
