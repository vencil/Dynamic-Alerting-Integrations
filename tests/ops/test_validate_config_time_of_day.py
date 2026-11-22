"""validate-config 與路由產生器經 da-guard 讀樹的結論，與執行時刻無關（#3883、#3835、#3918）。

`routes` 列（`generate-routes --validate`，含共用鍵判決與 custom-alert 送達檢查的每一行）、
`no_alert` 列、`platform_defaults` 列，以及產生器寫入模式（`--output-configmap
--strict`）的結束碼與 findings：同一棵樹在兩個時窗內與時窗外的時刻跑，都要逐行相同——只有
`expires:` 依執行時刻判。撞名時窗內整棵樹的 /metrics 收集不了，那一刻讀 served-values
一般模式的讀者會整份失敗；只要不吃時刻欄位的讀者改讀 `--config-facts`（不 resolve、不
Gather），要整天值的讀者讀 `--schedules-only`（#3918）。

這些讀取都不帶 `--at`（validate-config 與產生器沒有這個旗標），所以這裡把
`$DA_GUARD_BINARY` 換成一支包裝：對 `key-verdict` 與 `served-values` 補上固定的
`--at`（`--config-facts` 不收 `--at`，原樣轉），其餘原樣轉給真的 da-guard——等於在那個
時刻執行。⚠️ 主閘門等其他子命令沒有
`--at`，在這裡跑的是真實時鐘：這支測試量不到它們，只量經包裝的讀取；`--config-facts` 不收
`--at`，它自身會不會開始讀時刻也量不到（由 Go 端 `servedConfigFacts` 不吃時刻、與一般模式
逐欄相同的測試守）。租戶 id 是 fixture
名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import validate_config as vc
from factories import da_guard_at  # noqa: E402  (tests/ on sys.path: conftest)

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
    # 只有 page 模式 custom alert、沒有 `_routing` 的租戶（送達檢查，#1211），宣告一種 exporter。
    "tc.yaml": "tenants:\n  tc:\n    _expected_exporters: [mariadb]\n    _custom_alerts:\n"
               "      - recipe: threshold\n        name: q_high\n        metric: q_high_depth\n"
               "        op: \">\"\n        window: 5m\n        threshold: \"1000:warning\"\n",
}

#: 產生器組裝用的 base：firehose 沒有 integration（tc 的 page alert 無人收 → --strict 擋），
#: watchdog-heartbeat 有接（不讓它自己的 finding 混進來）。
_BASE = (
    "global:\n  resolve_timeout: 5m\nroute:\n  receiver: default\nreceivers:\n"
    "  - name: default\n  - name: custom-alerts-firehose\n"
    "  - name: watchdog-heartbeat\n    webhook_configs:\n"
    "      - url: \"https://deadman.example.com/ping\"\n")

_GAR = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"


def _tree(root: Path) -> str:
    for rel, text in _FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _rows_at(tmp_path: Path, monkeypatch, at: str) -> dict[str, dict]:
    real = os.environ["DA_GUARD_BINARY"]
    conf_d = _tree(tmp_path / f"tree-{at.replace(':', '')}")
    monkeypatch.setenv("DA_GUARD_BINARY", da_guard_at(tmp_path, at, ("key-verdict", "served-values"), real))
    try:
        verdict: list = []
        routes = vc.check_routes(conf_d, verdict_out=verdict)
        no_alert = vc.check_no_alert(conf_d, verdict[0] if verdict else None)
        platform = vc.check_platform_defaults(conf_d)
    finally:
        monkeypatch.setenv("DA_GUARD_BINARY", real)
    return {"routes": routes, "no_alert": no_alert, "platform_defaults": platform}


def test_the_rows_read_through_da_guard_are_the_same_inside_and_outside_a_window(tmp_path, monkeypatch):
    first, *rest = [_rows_at(tmp_path, monkeypatch, at) for at in _AT]
    for at, other in zip(_AT[1:], rest):
        for row in first:
            assert (first[row]["status"], first[row]["details"]) == \
                (other[row]["status"], other[row]["details"]), (row, _AT[0], at)
    inside = first
    routes = "\n".join(inside["routes"]["details"])
    # 收集不了的時段照樣判（撞名是主閘門 metrics_not_gatherable 的事），判決不會給不出來。
    assert "key verdict incomplete" not in routes, routes
    assert "unread `nodecl_pct` (no_row)" in routes, routes
    assert "dead_entry" not in routes and "disable_noop" not in routes, routes
    # 送達檢查在收集不了的那一刻也判了：tc 的 page alert 有被點名，沒有「not checked」。
    assert "tenant 'tc': 1 served page-mode custom alert(s) (q_high)" in routes, routes
    assert "custom-alert delivery not checked" not in routes, routes
    # 只在時窗內送出的鍵，整天都算送出：它的 critical 列沒有出貨告警比較。
    assert inside["no_alert"]["status"] == vc.WARN, inside["no_alert"]
    assert any(d.startswith("no_alert `clickhouse_queries_rate` (served as critical)")
               for d in inside["no_alert"]["details"]), inside["no_alert"]
    # platform_defaults 讀 effective（內建表與 key_sources，不讀時刻）：收集不了的那一刻也判了，
    # 不是「not checked」（#3832）。
    assert inside["platform_defaults"]["status"] == vc.PASS, inside["platform_defaults"]
    assert inside["platform_defaults"]["details"][0].startswith("built-in table "), \
        inside["platform_defaults"]


def _generate_at(tmp_path: Path, at: str) -> tuple[int, list]:
    """`generate-routes --output-configmap --strict` at *at*: (rc, findings as recorded)."""
    real = os.environ["DA_GUARD_BINARY"]
    tag = at.replace(":", "")
    conf_d = _tree(tmp_path / f"gen-{tag}")
    base = tmp_path / "base.yaml"
    base.write_text(_BASE, encoding="utf-8")
    findings = tmp_path / f"findings-{tag}.json"
    proc = subprocess.run(
        [sys.executable, "-s", str(_GAR), "--config-dir", conf_d, "--output-configmap",
         "-o", str(tmp_path / f"cm-{tag}.yaml"), "--base-config", str(base), "--strict",
         "--findings-json", str(findings)],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
        env={**os.environ, "DA_GUARD_BINARY": da_guard_at(tmp_path, at, ("key-verdict", "served-values"), real)})
    recorded = json.loads(findings.read_text(encoding="utf-8")) if findings.exists() else None
    assert recorded is not None, proc.stderr[-2000:]
    # The tree's path differs per run; the findings must not.
    return proc.returncode, json.loads(json.dumps(recorded).replace(conf_d, "<conf.d>")
                                       .replace(str(tmp_path / f"cm-{tag}.yaml"), "<out>"))


def test_the_generator_refuses_at_every_time_of_day(tmp_path):
    """#3918：送達檢查讀不到 custom alerts 就只印 NOTICE，於是撞名時窗內 `--strict` 放行、
    寫出 ConfigMap，時窗外才擋。整天讀之後兩個時刻都擋，findings 逐筆相同。"""
    first, *rest = [_generate_at(tmp_path, at) for at in _AT]
    for at, other in zip(_AT[1:], rest):
        assert first == other, (_AT[0], at)
    rc, findings = first
    assert rc == 1, findings
    assert any(f.get("kind") == "custom_alert_undelivered" for f in findings["findings"]), findings
