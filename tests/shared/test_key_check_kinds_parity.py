"""key check 的 kind：Go 定義一次，Python 讀者的封閉集合要跟它一樣（#3703）。

kind 在 `pkg/config/key_check.go` 定義（`// Key check kinds.` 常數區塊，`KeyCheckKinds()` 列全部）；
哪些是改名類由 `KeyCheckKind.IsRenamed` 的 `case` 決定。Python 讀 `key_checks` 時用
`_lib_tenant_values.KEY_CHECK_KINDS`／`KEY_CHECK_RENAMED_KINDS`（不認得的 kind 當成讀不懂的輸出；
改名類印 NOTICE，其餘印 WARN），產生器的 `_KEY_CHECK_KINDS` 再把每個 kind 對到 `--findings-json`
的 kind。新增一個 kind 卻漏掉 Python 這邊，CI 的讀者會在第一次遇到它時整份判決讀不懂；這支測試讓它在
合入前就紅。`tests/ops/test_key_check_canary_matrix.py` 的矩陣也由這個集合推導。

讀 Go 原始碼而不是另放清單：找不到常數區塊或 `IsRenamed`、或一個常數都沒讀到，都算失敗——讀不到不能當成一致。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))

import _lib_tenant_values as tv  # noqa: E402
from _grar_merge import FINDING_KINDS  # noqa: E402
from generate_alertmanager_routes import _KEY_CHECK_KINDS  # noqa: E402

KEY_CHECK_GO = (REPO / "components" / "threshold-exporter" / "app" / "pkg" / "config"
                / "key_check.go")


def _go_consts() -> dict[str, str]:
    """Constant name → kind, from the `// Key check kinds.` block."""
    src = KEY_CHECK_GO.read_text(encoding="utf-8")
    m = re.search(r"^// Key check kinds\.\nconst \((.*?)^\)\n", src, re.S | re.M)
    assert m, f"the `// Key check kinds.` const block is not in {KEY_CHECK_GO}"
    body = re.sub(r"//[^\n]*", "", m.group(1))
    consts = dict(re.findall(r"^\s*(KeyCheck\w+)\s+KeyCheckKind\s*=\s*\"([a-z_]+)\"\s*$", body, re.M))
    assert consts, f"no KeyCheck constant read from {KEY_CHECK_GO}"
    # Every line of the block is a constant this read took.
    assert len([ln for ln in body.splitlines() if ln.strip()]) == len(consts), body
    return consts


def _go_listed(func: str, pattern: str) -> list[str]:
    src = KEY_CHECK_GO.read_text(encoding="utf-8")
    m = re.search(pattern, src, re.S | re.M)
    assert m, f"{func} is not in {KEY_CHECK_GO} in the shape this test reads"
    names = re.findall(r"\bKeyCheck\w+\b", m.group(1))
    assert names, f"no constant read from {func}"
    return names


def test_key_check_kinds_lists_every_constant():
    consts = _go_consts()
    listed = _go_listed("KeyCheckKinds", r"^func KeyCheckKinds\(\) \[\]KeyCheckKind \{\n\treturn "
                                         r"\[\]KeyCheckKind\{(.*?)\}\n\}")
    assert sorted(listed) == sorted(consts), (listed, sorted(consts))


def test_python_reader_takes_exactly_the_go_kinds():
    assert set(tv.KEY_CHECK_KINDS) == set(_go_consts().values())


def test_python_renamed_kinds_are_go_is_renamed():
    consts = _go_consts()
    renamed = _go_listed("KeyCheckKind.IsRenamed",
                         r"^func \(k KeyCheckKind\) IsRenamed\(\) bool \{\n\tswitch k \{\n\tcase (.*?):\n")
    assert set(tv.KEY_CHECK_RENAMED_KINDS) == {consts[n] for n in renamed}


def test_route_generator_maps_every_kind():
    assert set(_KEY_CHECK_KINDS) == set(tv.KEY_CHECK_KINDS)
    missing = sorted(set(_KEY_CHECK_KINDS.values()) - set(FINDING_KINDS))
    assert not missing, f"FINDING_KINDS has no entry for {missing}"
