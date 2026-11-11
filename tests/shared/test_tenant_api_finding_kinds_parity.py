"""tenant-api 記 log 時認得的 finding kind，就是路由產生器的 FINDING_KINDS（#1560 ⑤-b）。

tenant-api 的 log 只記產生器 finding 的 kind 與數量，而 kind 也是容器給的字串：
不在封閉清單裡的一律記成 `other`（`internal/generator/finding_kinds.go` 的
`findingKinds`）。清單的 SSOT 是 `_grar_merge.FINDING_KINDS`；Go 那份是複本，
這支測試把兩份釘在一起。產生器多一個 kind 而 Go 沒跟上，log 只會少一點資訊，
不會漏字；但兩份不一致就是有人忘了，所以在合入前讓它紅。

讀 Go 原始碼而不是另放清單：找不到 map 字面值、或一個 kind 都沒讀到，都算失敗。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))

from _grar_merge import FINDING_KINDS  # noqa: E402

GO = REPO / "components" / "tenant-api" / "internal" / "generator" / "finding_kinds.go"


def _go_kinds() -> set[str]:
    src = GO.read_text(encoding="utf-8")
    m = re.search(r"^var findingKinds = map\[string\]bool\{\n(.*?)^\}\n", src, re.S | re.M)
    assert m, f"`var findingKinds = map[string]bool{{` is not in {GO}"
    kinds = set(re.findall(r'^\s*"([a-z0-9_]+)":\s*true,\s*$', m.group(1), re.M))
    assert kinds, f"no kind read from {GO}"
    # Every line of the literal is a row: a line the pattern skips is a
    # row this test would silently not compare.
    rows = [line for line in m.group(1).splitlines() if line.strip()]
    assert len(rows) == len(kinds), rows
    return kinds


def test_tenant_api_logs_exactly_the_generators_kinds():
    assert _go_kinds() == set(FINDING_KINDS)
