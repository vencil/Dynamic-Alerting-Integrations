"""validate-config 讀共用鍵判決的兩列，結論與執行時刻無關（#3883、#3835）。

`routes` 列（`generate-routes --validate`，含共用鍵判決的每一行）與 `no_alert` 列，
都經 da-guard 讀樹；判決改成依整天判之後，同一棵樹在兩個時窗內與時窗外的時刻跑，兩列的
狀態與每一行都要相同——只有 `expires:` 依執行時刻判。

兩列都不帶 `--at`（validate-config 沒有這個旗標），所以這裡把 `$DA_GUARD_BINARY` 換成
一支包裝：對 `key-verdict` 與 `served-values` 補上固定的 `--at`，其餘原樣轉給真的
da-guard——等於在那個時刻執行。租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import validate_config as vc

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env", "da_read_env")

#: 撞名時窗內、no_alert 鍵的時窗內、兩者之外。
_AT = ("2026-11-13T01:15:00Z", "2026-11-13T01:45:00Z", "2026-11-13T12:00:00Z")

_FILES = {
    "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n  clickhouse_queries_rate: 80\n",
    # 時窗內兩鍵送出同一條 series（整棵樹的 /metrics 收集不了）；時窗外 `_critical` 是 disable。
    "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70:critical\"\n"
               "    mysql_connection_usage_percent_critical:\n      default: disable\n"
               "      overrides:\n        - window: \"01:00-01:29\"\n          value: \"95\"\n",
    # 只在另一個時窗內以 critical 送出、出貨告警不比較的鍵（no_alert；撞名時段整棵樹收集
    # 不了，什麼都沒送，所以錯開）；根 defaults 沒宣告的鍵，時窗外 disable、時窗內沒有列，
    # 另帶一個被遮住的 window。
    "tb.yaml": "tenants:\n  tb:\n    clickhouse_queries_rate:\n      default: disable\n"
               "      overrides:\n        - window: \"01:30-01:59\"\n          value: \"95:critical\"\n"
               "    nodecl_pct:\n      default: disable\n      overrides:\n"
               "        - window: \"01:00-01:59\"\n          value: \"5\"\n"
               "        - window: \"01:10-01:20\"\n          value: \"6\"\n",
}


def _tree(root: Path) -> str:
    for rel, text in _FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _at_wrapper(tmp_path: Path, real: str, at: str) -> str:
    """A da-guard that runs `key-verdict` / `served-values` at *at*."""
    path = tmp_path / f"da-guard-at-{at.replace(':', '')}"
    path.write_text(
        "#!/bin/sh\n"
        f"real='{real}'\n"
        'case "$1" in\n'
        f"  key-verdict|served-values) exec \"$real\" \"$@\" --at '{at}' ;;\n"
        '  *) exec "$real" "$@" ;;\n'
        "esac\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _rows_at(tmp_path: Path, monkeypatch, at: str) -> dict[str, dict]:
    real = os.environ["DA_GUARD_BINARY"]
    conf_d = _tree(tmp_path / f"tree-{at.replace(':', '')}")
    monkeypatch.setenv("DA_GUARD_BINARY", _at_wrapper(tmp_path, real, at))
    try:
        verdict: list = []
        routes = vc.check_routes(conf_d, verdict_out=verdict)
        no_alert = vc.check_no_alert(conf_d, verdict[0] if verdict else None)
    finally:
        monkeypatch.setenv("DA_GUARD_BINARY", real)
    return {"routes": routes, "no_alert": no_alert}


#: ⚠️ 不在共用判決裡、仍以執行時刻讀的一行：`routes` 列的 custom-alert 送達檢查（#1211）讀
#: served-values 當下的 custom alerts，那一刻 /metrics 收集不了就只印這行 NOTICE
#: （`blocks: never`，不改列的狀態）。這支測試不比它，只比其餘每一行。
_INSTANT_NOTICE = "NOTICE: custom-alert delivery not checked:"


def _lines(row: dict) -> list[str]:
    return [d for d in row["details"] if not d.startswith(_INSTANT_NOTICE)]


def test_routes_and_no_alert_rows_are_the_same_inside_and_outside_a_window(tmp_path, monkeypatch):
    first, *rest = [_rows_at(tmp_path, monkeypatch, at) for at in _AT]
    for at, other in zip(_AT[1:], rest):
        for row in ("routes", "no_alert"):
            assert (first[row]["status"], _lines(first[row])) == \
                (other[row]["status"], _lines(other[row])), (row, _AT[0], at)
    inside = first
    routes = "\n".join(inside["routes"]["details"])
    # 收集不了的時段照樣判（撞名是主閘門 metrics_not_gatherable 的事），判決不會給不出來。
    assert "key verdict incomplete" not in routes, routes
    assert "unread `nodecl_pct` (no_row)" in routes, routes
    assert "dead_entry" not in routes and "disable_noop" not in routes, routes
    # 只在時窗內送出的鍵，整天都算送出：它的 critical 列沒有出貨告警比較。
    assert inside["no_alert"]["status"] == vc.WARN, inside["no_alert"]
    assert any(d.startswith("no_alert `clickhouse_queries_rate` (served as critical)")
               for d in inside["no_alert"]["details"]), inside["no_alert"]
