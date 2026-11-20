"""evaluate-policy 與 validate-config 的 Policy-as-Code 列只讀整天值，結論與執行時刻無關（#4073）。

兩者共用 `policy_engine.load_policy_inputs`。它原本讀 `served-values --schedules`，也就是
一般模式再加整天：`--at` 落在 /metrics 收集不了的時窗內時，一般模式那一步就 rc 2，訊息只說
整棵樹收集不了、不點名時段；時窗外則由某個 segment 的 error 點名，兩次說法不同。改讀
`--schedules-only` 後，收集不了的時段由文件層 `ungathered` 點名，不論幾點都 rc 2、說法相同。
`required` 分辨「有設定但整天不發」與「沒設定」，讀的是 `--schedules-only` 的
`unserved_thresholds`，不再是 `--at` 那一刻的 `unserved`。

`$DA_GUARD_BINARY` 換成一支包裝，對 `served-values` 補上固定的 `--at`（`--config-facts`
原樣轉），等於在那個時刻執行；其餘子命令讀真實時鐘，這裡量不到它們。租戶 id 是 fixture 名
（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import policy_engine as pe
import validate_config as vc
from _lib_exitcodes import emit_result  # noqa: E402
from factories import da_guard_at  # noqa: E402  (tests/ on sys.path: conftest)

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_crdecode_env", "da_read_env")

#: 收集不了的時窗內、只在另一個時窗送出的鍵的時窗內、兩者之外。
_AT = ("2026-11-13T01:15:00Z", "2026-11-13T01:45:00Z", "2026-11-13T12:00:00Z")

_POLICIES = (
    "_policies:\n"
    "  - {name: ch-required, target: clickhouse_queries_rate, operator: required}\n"
    "  - {name: nodecl-required, target: nodecl_pct, operator: required}\n"
    "  - {name: mysql-cap, target: mysql_connection_usage_percent, operator: lte, value: 75}\n"
)

#: 整天都收集得到的樹。tb：clickhouse 只在 01:30-01:59 送出，nodecl_pct 整天 disable。
_GATHERED = {
    "_defaults.yaml": "defaults:\n  mysql_connection_usage_percent: 80\n  clickhouse_queries_rate: 80\n"
                      + _POLICIES,
    "tb.yaml": "tenants:\n  tb:\n    clickhouse_queries_rate:\n      default: disable\n"
               "      overrides:\n        - window: \"01:30-01:59\"\n          value: \"95\"\n"
               "    nodecl_pct:\n      default: disable\n",
}

#: 加上 ta：01:00-01:29 兩鍵送出同一條 series，那段整棵樹的 /metrics 收集不了。
_UNGATHERED = {
    **_GATHERED,
    "ta.yaml": "tenants:\n  ta:\n    mysql_connection_usage_percent: \"70:critical\"\n"
               "    mysql_connection_usage_percent_critical:\n      default: disable\n"
               "      overrides:\n        - window: \"01:00-01:29\"\n          value: \"95\"\n",
}


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _at(tmp_path: Path, monkeypatch, at: str, files: dict[str, str], capsys):
    """(evaluate-policy 的 rc、--json 文件、stderr；validate-config 的 policy_dsl 列) at *at*."""
    real = os.environ["DA_GUARD_BINARY"]
    conf_d = _tree(tmp_path / f"tree-{at.replace(':', '')}", files)
    monkeypatch.setenv("DA_GUARD_BINARY", da_guard_at(tmp_path, at, ("served-values",), real))
    try:
        capsys.readouterr()
        rc = emit_result(pe.main(["--config-dir", conf_d, "--json", "--ci"]))
        out = capsys.readouterr()
        row = vc.check_policy_dsl(conf_d)
    finally:
        monkeypatch.setenv("DA_GUARD_BINARY", real)
    doc = json.loads(out.out) if out.out.strip() else None
    return rc, doc, out.err.replace(conf_d, "<conf.d>"), {
        "status": row["status"], "details": [d.replace(conf_d, "<conf.d>") for d in row["details"]]}


def test_the_violations_are_the_same_at_every_time_of_day(tmp_path, monkeypatch, capsys):
    first, *rest = [_at(tmp_path, monkeypatch, at, _GATHERED, capsys) for at in _AT]
    rc, doc, _err, row = first
    for at, (o_rc, o_doc, _e, o_row) in zip(_AT[1:], rest):
        assert (rc, doc["violations"]) == (o_rc, o_doc["violations"]), (_AT[0], at)
        assert row == o_row, (_AT[0], at)
    assert rc == 1, doc
    by_rule = {v["rule_name"]: v["message"] for v in doc["violations"]}
    # 整天 disable 的鍵是「有設定但不發」，不是「沒設定」：讀的是整天的 unserved_thresholds。
    assert "已配置（值: disable）但 exporter 不發出" in by_rule["nodecl-required"], by_rule
    # 只在 01:30-01:59 送出的鍵，其餘時段沒有列。
    assert "00:00-01:30" in by_rule["ch-required"] and "01:59-24:00" in by_rule["ch-required"], by_rule
    assert "mysql-cap" in by_rule, by_rule
    assert row["status"] == vc.FAIL, row


def test_an_ungathered_part_of_the_day_is_named_at_every_time_of_day(tmp_path, monkeypatch, capsys):
    """收集不了的時段裡什麼都不知道，所以整天都不判：不論 `--at` 落在時窗內外都 rc 2，說法相同、
    點名時段；validate-config 的列同樣 FAIL 並點名，不是 PASS。"""
    runs = [_at(tmp_path, monkeypatch, at, _UNGATHERED, capsys) for at in _AT]
    for at, (rc, doc, err, row) in zip(_AT, runs):
        assert rc == 2 and doc is None, (at, rc, doc)
        assert "cannot be gathered in part of the day, so nothing is judged: 01:00-01:29 UTC" in err, (at, err)
        assert row["status"] == vc.FAIL, (at, row)
        assert any("01:00-01:29 UTC" in d for d in row["details"]), (at, row)
    assert len({(r[2], json.dumps(r[3])) for r in runs}) == 1, runs
