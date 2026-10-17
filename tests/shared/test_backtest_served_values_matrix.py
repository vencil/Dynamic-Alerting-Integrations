"""`backtest --config-dir/--baseline` 找到的門檻變更要與 Go 一致（#2119）：兩棵樹各自由
da-guard served-values（/metrics）讀，逐租戶、逐條 /metrics series 比 exporter 送出的值
（#3111：以 series 為單位，不以 threshold key 為單位）。

step 0 的政策：Python 工具不自己解讀 YAML 語意，值一律來自 Go。`extract_changes_from_dirs`
原本用 PyYAML 重讀兩個目錄、比租戶檔的頂層 key。修正前（main 6447be54）實測：

- 只改 `_platform.yaml` 的 `tenants.tx.mysql_connections`（50 → 60）：回 0 個變更；
  exporter 對新樹送 60。
- 租戶檔是 `tenants:` 包裝格式（conf.d 的標準格式）：回 1 個 metric=`tenants`、值是整個
  dict 字串的變更，而不是 `mysql_connections`。
- 平鋪的租戶檔（`mysql_connections: "50"`，沒有 `tenants:`）：回 1 個變更；exporter 不從
  這種檔送任何租戶（#2115 R3），改它 /metrics 不變。

每格比對：`extract_changes_from_dirs` 的答案 == 直接對兩棵樹跑 served-values、在同一個
`at` 逐租戶、逐條 series（名稱加完整 label set）比值得到的答案（一邊沒送的 series 或租戶記
None；舊拼法的 twin 不算一條）。排程只在一天
其他時段不同的 key 另有幾格釘死數字與 `window`（每一段不同都要列出，不只第一段；同一組
新舊值只算一個變更，`window` 列出它成立的所有時段）。
da-guard 拒收的樹、或某時段 /metrics 收不到，都要 exit 2。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKTEST = REPO_ROOT / "scripts" / "tools" / "ops" / "backtest_threshold.py"
sys.path.insert(0, str(BACKTEST.parent))
import backtest_threshold as bt  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")

AT = "2026-10-08T12:00:00Z"

_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slow: 90\n"
_K = {"keys": ["mysql_connections"]}
_TX_EMPTY = "tenants:\n  tx: {}\n"


def _wrapper(value: str) -> str:
    return f"tenants:\n  tx:\n    mysql_connections: \"{value}\"\n"


def _scheduled(night: str) -> str:
    return ("tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n"
            f"      overrides:\n        - window: \"01:00-09:00\"\n          value: \"{night}\"\n")


# (name, baseline files, current files)
SHAPES = [
    ("platform-only",
     {"_defaults.yaml": _DEFAULTS, "_platform.yaml": "tenants:\n  tx: {mysql_connections: \"50\"}\n",
      "tx.yaml": _TX_EMPTY},
     {"_defaults.yaml": _DEFAULTS, "_platform.yaml": "tenants:\n  tx: {mysql_connections: \"60\"}\n",
      "tx.yaml": _TX_EMPTY}),
    ("wrapper-tenant",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")},
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("60")}),
    # The exporter serves no tenant from a file without `tenants:` (#2115 R3).
    ("flat-file-not-served",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": "mysql_connections: \"50\"\n"},
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": "mysql_connections: \"60\"\n"}),
    ("defaults-layer",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _TX_EMPTY},
     {"_defaults.yaml": "defaults:\n  mysql_connections: 85\n  mysql_slow: 90\n",
      "tx.yaml": _TX_EMPTY}),
    ("subdirectory-tenant",
     {"_defaults.yaml": _DEFAULTS, "team/tx.yaml": _wrapper("50")},
     {"_defaults.yaml": _DEFAULTS, "team/tx.yaml": _wrapper("60")}),
    ("profile",
     {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": "profiles:\n  gold:\n    mysql_slow: 60\n",
      "_platform.yaml": "tenants:\n  tx: {_profile: gold}\n", "tx.yaml": _TX_EMPTY},
     {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": "profiles:\n  gold:\n    mysql_slow: 40\n",
      "_platform.yaml": "tenants:\n  tx: {_profile: gold}\n", "tx.yaml": _TX_EMPTY}),
    ("unchanged",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")},
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")}),
    ("tenant-added",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")},
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50"), "ty.yaml": "tenants:\n  ty: {}\n"}),
    ("key-disabled",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")},
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("disable")}),
]


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body, encoding="utf-8")
    return root


def _text(v: float | None) -> str | None:
    if v is None:
        return None
    return str(int(v)) if float(v).is_integer() else repr(v)


def _alert(s: "tv.Series") -> str:
    """The threshold key whose alert compares a row (#3017): critical → `_critical`."""
    return s.metric_key + "_critical" if s.labels.get("severity") == "critical" else s.metric_key


def _rows_at(conf_d: Path) -> dict[tuple, list]:
    """{(tenant, (name, labels)): [value, {keys}, series]}: every /metrics row
    (not a legacy twin) served at AT, with the threshold keys serving it."""
    out: dict[tuple, list] = {}
    for t, v in tv.load_served_values(conf_d, at=AT).items():
        for k, series in v.series.items():
            for s in series:
                if not s.legacy_twin:
                    row = out.setdefault((t, (s.name, tuple(sorted(s.labels.items())))),
                                         [_text(v.values[k]), set(), s])
                    row[1].add(k)
    return out


def _oracle(base: Path, cur: Path) -> list[dict]:
    old, new = _rows_at(base), _rows_at(cur)
    out = []
    for t, ident in set(old) | set(new):
        o, n = old.get((t, ident), [None, set(), None]), new.get((t, ident), [None, set(), None])
        if o[0] != n[0]:
            out.append(((t, _alert(o[2] or n[2]), ident), {
                "tenant": t, "metric": _alert(o[2] or n[2]), "old_value": o[0],
                "new_value": n[0], "keys": sorted(o[1] | n[1])}))
    return [row for _order, row in sorted(out, key=lambda r: r[0])]


def _series_at(conf_d: Path) -> dict[tuple, "tv.Series"]:
    """{(tenant, alert key, old?, new?): series} — the row each oracle change is about."""
    return {(t, _alert(row[2])): row[2] for (t, _i), row in _rows_at(conf_d).items()}


def _backtest(monkeypatch: pytest.MonkeyPatch, base: Path, cur: Path) -> list[dict]:
    monkeypatch.setattr(bt, "now_at", lambda: AT, raising=False)
    return bt.extract_changes_from_dirs(str(cur), str(base))


def _no_series(changes: list[dict]) -> list[dict]:
    """The changes without `series` (pinned on its own where a test is about it)."""
    return [{k: v for k, v in c.items() if k != "series"} for c in changes]


def test_matrix_is_not_vacuous(tmp_path: Path) -> None:
    """Pinned numbers on the Go side, so equal readings mean something."""
    got = {}
    for name, base, cur in SHAPES:
        got[name] = _oracle(_tree(tmp_path / name / "base", base), _tree(tmp_path / name / "cur", cur))
    assert got["platform-only"] == [{"tenant": "tx", "metric": "mysql_connections",
                                     "old_value": "50", "new_value": "60", **_K}]
    assert got["defaults-layer"] == [{"tenant": "tx", "metric": "mysql_connections",
                                      "old_value": "80", "new_value": "85", **_K}]
    assert got["profile"] == [{"tenant": "tx", "metric": "mysql_slow",
                               "old_value": "60", "new_value": "40", "keys": ["mysql_slow"]}]
    assert got["flat-file-not-served"] == [] and got["unchanged"] == []
    assert {(c["tenant"], c["old_value"]) for c in got["tenant-added"]} == {("ty", None)}
    assert got["key-disabled"] == [{"tenant": "tx", "metric": "mysql_connections",
                                    "old_value": "50", "new_value": None, **_K}]


@pytest.mark.parametrize("name,base,cur", SHAPES, ids=[n for n, _, _ in SHAPES])
def test_backtest_agrees_with_the_exporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           name: str, base: dict, cur: dict) -> None:
    b, c = _tree(tmp_path / "base", base), _tree(tmp_path / "cur", cur)
    got = _backtest(monkeypatch, b, c)
    assert _no_series(got) == _oracle(b, c), name
    # #2750 / #3111: each change carries the one /metrics row it is about.
    rows = {**_series_at(b), **_series_at(c)}
    for change in got:
        assert change["series"] == [rows[(change["tenant"], change["metric"])]], (name, change)


def _two_windows(night: str, evening: str) -> str:
    return ("tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n"
            f"        - window: \"01:00-02:00\"\n          value: \"{night}\"\n"
            f"        - window: \"20:00-21:00\"\n          value: \"{evening}\"\n")


def test_a_change_outside_the_instant_is_named_with_its_window(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal at AT (12:00 UTC), different 01:00-09:00: still a change, carrying
    the values of that part of the day and naming it."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("1000")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("2000")})
    assert _oracle(b, c) == []  # the instant alone would miss it
    assert _no_series(_backtest(monkeypatch, b, c)) == [{
        "tenant": "tx", "metric": "mysql_connections",
        "old_value": "1000", "new_value": "2000", "window": ["01:00-09:00"], **_K}]


def test_every_differing_part_of_the_day_is_a_row(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two overrides both changed: two rows, not the first one only (round-1
    review: the 20:00 change used to be dropped)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1000", "5")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1001", "99999")})
    assert _no_series(_backtest(monkeypatch, b, c)) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "1000", "new_value": "1001",
         "window": ["01:00-02:00"], **_K},
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "5", "new_value": "99999",
         "window": ["20:00-21:00"], **_K}]


def test_one_pair_in_several_parts_of_the_day_is_one_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backtested once over the whole lookback, so counted once (round-2
    review: a 22:00-02:00 override used to make two HIGH rows)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1000", "1000")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("2000", "2000")})
    assert _no_series(_backtest(monkeypatch, b, c)) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "1000", "new_value": "2000",
         "window": ["01:00-02:00", "20:00-21:00"], **_K}]
    over_midnight = ("tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n"
                     "      overrides:\n        - window: \"22:00-02:00\"\n          value: \"{v}\"\n")
    b = _tree(tmp_path / "base2", {"_defaults.yaml": _DEFAULTS, "tx.yaml": over_midnight.format(v=1000)})
    c = _tree(tmp_path / "cur2", {"_defaults.yaml": _DEFAULTS, "tx.yaml": over_midnight.format(v=5)})
    assert _no_series(_backtest(monkeypatch, b, c)) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "1000", "new_value": "5",
         "window": ["00:00-02:00", "22:00-24:00"], **_K}]


def test_a_severity_window_is_a_row_of_its_own(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The current side serves 75 all day, as critical 01:00-09:00 (#3111):
    the warning row is 70 -> 75 outside that window and stops being served
    inside it, and the critical row starts being served inside it — each a
    change of the row its own alert compares."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
        "tenants:\n  tx:\n    mysql_connections:\n      default: \"75\"\n      overrides:\n"
        "        - window: \"01:00-09:00\"\n          value: \"75:critical\"\n")})
    t = tv.load_served_tree(c, at=AT, schedules=True).tenants["tx"]
    assert len(t.schedules["mysql_connections"].segments) == 3  # not vacuous
    got = _backtest(monkeypatch, b, c)
    warning = {"tenant": "tx", "metric": "mysql_connections", **_K}
    assert _no_series(got) == [
        {**warning, "old_value": "70", "new_value": "75", "window": ["00:00-01:00", "09:00-24:00"]},
        {**warning, "old_value": "70", "new_value": None, "window": ["01:00-09:00"]},
        {"tenant": "tx", "metric": "mysql_connections_critical", "old_value": None,
         "new_value": "75", **_K, "window": ["01:00-09:00"]}]
    assert [ch["series"] for ch in got] == [[_ident("mysql_connections", "warning")]] * 2 + [
        [_ident("mysql_connections", "critical")]]


def _ident(metric_key: str, severity: str, component: str = "mysql",
           metric: str = "connections", twin: bool = False) -> "tv.Series":
    """The identity served-values gives a plain tx key (pinned, #2750)."""
    return tv.Series("user_threshold", {"tenant": "tx", "component": component, "metric": metric,
                                        "severity": severity}, metric_key, twin, {}, {})


@pytest.mark.parametrize("value", ["70:high", "70:", "70:a|b"])
def test_a_severity_no_rule_selects_is_served_as_the_default(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """#3071: a severity no threshold rule selects makes the value
    unreadable; the exporter serves the platform default (80, warning), so
    the change is 70 -> 80. #3060 read `"70:"` as served with severity ""
    (and before that, da-guard left `severity` out of such a segment and
    backtest exited 2); no current exporter serves such a row."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper(value)})
    segs = tv.load_served_tree(c, at=AT, schedules=True).tenants["tx"].schedules["mysql_connections"].segments
    assert [s[:4] for s in segs] == [("00:00", "24:00", 80.0, "warning")]
    got = _backtest(monkeypatch, b, c)
    assert _no_series(got) == [{"tenant": "tx", "metric": "mysql_connections",
                                "old_value": "70", "new_value": "80", **_K}]
    assert got[0]["series"] == [_ident("mysql_connections", "warning")]


def test_a_severity_only_change_is_two_rows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#3111: the same value under another severity is another /metrics row,
    compared by another alert (#3060): the warning row stops being served,
    the critical row starts."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70:Critical")})
    got = _backtest(monkeypatch, b, c)
    assert _no_series(got) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "70", "new_value": None, **_K},
        {"tenant": "tx", "metric": "mysql_connections_critical", "old_value": None,
         "new_value": "70", **_K}]
    assert [ch["series"] for ch in got] == [[_ident("mysql_connections", "warning")],
                                            [_ident("mysql_connections", "critical")]]


def test_a_severity_only_part_of_the_day_is_two_rows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """70 all day on both sides, critical 01:00-09:00 on the current one: in
    that window the warning row stops and the critical row starts."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("70:critical")})
    assert _no_series(_backtest(monkeypatch, b, c)) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "70", "new_value": None,
         **_K, "window": ["01:00-09:00"]},
        {"tenant": "tx", "metric": "mysql_connections_critical", "old_value": None,
         "new_value": "70", **_K, "window": ["01:00-09:00"]}]


def test_opposite_severity_changes_are_four_rows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning -> critical outside 01:00-09:00 and critical -> warning inside
    it: each row stops in the parts of the day it moved out of and starts in
    the ones it moved into."""
    def tree(name: str, default: str, night: str) -> Path:
        return _tree(tmp_path / name, {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
            f"tenants:\n  tx:\n    mysql_connections:\n      default: \"{default}\"\n"
            f"      overrides:\n        - window: \"01:00-09:00\"\n          value: \"{night}\"\n")})
    got = _no_series(_backtest(monkeypatch, tree("base", "70", "70:critical"),
                               tree("cur", "70:critical", "70")))
    day, night = ["00:00-01:00", "09:00-24:00"], ["01:00-09:00"]
    w = {"tenant": "tx", "metric": "mysql_connections", **_K}
    c = {"tenant": "tx", "metric": "mysql_connections_critical", **_K}
    assert got == [
        {**w, "old_value": "70", "new_value": None, "window": day},
        {**w, "old_value": None, "new_value": "70", "window": night},
        {**c, "old_value": None, "new_value": "70", "window": day},
        {**c, "old_value": "70", "new_value": None, "window": night}]


@pytest.mark.parametrize("old,new,status,risk,message,rc", [
    # #3071: no rule selects "high" (or ""), so the value is unreadable and
    # the default (80) is served: a value change, 70 -> 80, and back. #3060
    # R1 read these as the alert silenced (to-*: MEDIUM, rc 0) and enabled
    # again (from-high: HIGH, rc 1).
    ("70", "70:high", "analyzed", "LOW", "0 fewer firing points (+0.0%)", 0),
    ("70", "70:", "analyzed", "LOW", "0 fewer firing points (+0.0%)", 0),
    ("70:high", "70", "analyzed", "LOW", "0 fewer firing points (+0.0%)", 0),
    ("70", "60:high", "analyzed", "LOW", "0 fewer firing points (+0.0%)", 0),
], ids=["to-high", "to-empty", "from-high", "value-to-high"])
def test_a_severity_only_change_is_backtested_by_r1(
        tmp_path: Path, monkeypatch, capsys, cli_argv, old: str, new: str, status: str,
        risk: str, message: str, rc: int) -> None:
    """#3060 R1 (owner ruling): the severity decides which alert compares a
    row, the value when it fires. #3071 (owner ruling): a severity no rule
    selects is an unreadable value, served as the default, so those rows are
    value changes (exit codes 0, measured on the stub series)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper(old)})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper(new)})
    got_rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["status"], r["risk"], r["message"]) for r in rows] == [(status, risk, message)]
    assert "old_severity" not in rows[0] and "new_severity" not in rows[0]
    assert asked == ['tenant:mysql_threads_connected:max{tenant="tx"}']
    assert got_rc == rc


def test_to_critical_is_the_warning_alert_silenced_and_the_critical_one_enabled(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """#3111: `70` -> `70:critical` is backtested as two rows on one series
    (the stub's 50 then 2000): the warning alert loses its threshold (MEDIUM)
    and the critical alert starts comparing 70 (1 of 2 points: HIGH, rc 1).
    #3060 R1 replayed it as one row, the same points firing (LOW, rc 0)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70:critical")})
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["risk"], r["message"]) for r in rows] == [
        ("mysql_connections", "MEDIUM", "Metric disabled — all alerts silenced"),
        ("mysql_connections_critical", "HIGH", "Metric newly enabled — 1/2 points would fire")]
    assert asked == ['tenant:mysql_threads_connected:max{tenant="tx"}'] * 2
    assert rc == 1


def test_reports_name_the_keys_serving_a_row(tmp_path: Path, monkeypatch, capsys,
                                             cli_argv) -> None:
    """A row served by keys other than the one its metric names says so in
    both reports (and `keys` in --json)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("90:critical")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
        "tenants:\n  tx:\n    mysql_connections: disable\n    mysql_connections_critical: \"95\"\n")})
    md = tmp_path / "r.md"
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert ("tx/mysql_connections_critical: 90 -> 95 "
            "(served by: mysql_connections, mysql_connections_critical)") in out.out
    assert ("`mysql_connections_critical` (served by: `mysql_connections`, "
            "`mysql_connections_critical`) | 90 | 95 |") in md.read_text(encoding="utf-8")


@pytest.mark.parametrize("key,text,cell", [
    ("a|b", "a|b", "`a\\|b`"),            # a `|` would add a Markdown column
    ("a\x1bb", "a?b", "`a?b`"),            # YAML's `\x1b`: not printed raw
])
def test_a_key_from_the_tree_is_escaped(
        tmp_path: Path, monkeypatch, capsys, cli_argv, key: str, text: str,
        cell: str) -> None:
    """The keys are read from served-values: escaped (`safe_label`) in both
    reports, and a `|` escaped in the Markdown table, which keeps its 7
    columns."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("75")})
    extract = bt.extract_changes_from_dirs

    def served_by_another_key(*args, **kwargs):
        changes = extract(*args, **kwargs)
        for ch in changes:
            ch["keys"] = [key.encode().decode("unicode_escape")]
        return changes
    monkeypatch.setattr(bt, "extract_changes_from_dirs", served_by_another_key)
    md = tmp_path / "r.md"
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert f"(served by: {text})" in out.out and "\x1b" not in out.out
    row = next(line for line in md.read_text(encoding="utf-8").splitlines() if cell in line)
    assert len(re.findall(r"(?<!\\)\|", row)) == 8  # 7 columns


def test_a_critical_segment_outside_the_changed_pair_does_not_skip_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Severity is read only over the parts of the day the changed pair holds
    in: an unchanged critical override elsewhere leaves the change backtested."""
    override = ("      overrides:\n        - window: \"01:00-09:00\"\n"
                "          value: \"70:critical\"\n")
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
        "tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n" + override)})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
        "tenants:\n  tx:\n    mysql_connections:\n      default: \"75\"\n" + override)})
    assert _no_series(_backtest(monkeypatch, b, c)) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "70", "new_value": "75",
         "window": ["00:00-01:00", "09:00-24:00"], **_K}]


def test_a_schedule_against_a_plain_value_is_one_row_per_pair(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pair holding either side of the override is one row, both parts
    in its `window`; the override's own pair is another."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("1000")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("75")})
    got = [(r["window"], r["old_value"], r["new_value"]) for r in _backtest(monkeypatch, b, c)]
    assert got == [(["00:00-01:00", "09:00-24:00"], "70", "75"),
                   (["01:00-09:00"], "1000", "75")]


def test_skipped_files_are_named_once_with_their_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A file the load serves no tenant from is a WARN (#2115 R3): once when
    both trees carry it, else with the flag of the tree that does."""
    flat = "mysql_connections: \"50\"\n"
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50"),
                                  "both.yaml": flat})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50"),
                                 "both.yaml": flat, "new.yaml": flat})
    _backtest(monkeypatch, b, c)
    err = capsys.readouterr().err
    assert err.count("both.yaml") == 1 and "WARN: [both trees] both.yaml" in err, err
    assert err.count("new.yaml") == 1 and "WARN: [--config-dir] new.yaml" in err, err


def test_a_part_of_the_day_metrics_cannot_gather_fails_closed() -> None:
    """A segment carrying da-guard's error is refused, not read as "no row"."""
    seg = tv.ScheduleSegment("00:00", "24:00", None, None, "gather failed")
    values = tv.TenantValues("tx", {}, {}, {}, {}, {"k": tv.KeySchedule([seg], None, None)}, {}, {}, {})
    with pytest.raises(tv.ServedValuesError, match="gather failed"):
        bt._served_rows(values, "current")


def _run_main(monkeypatch, capsys, cli_argv, base: Path, cur: Path, *extra: str,
              has_data=lambda q: True):
    """`main` in-process with Prometheus answered by a stub series (50 then
    2000) for every query `has_data` accepts, nothing for the others."""
    monkeypatch.setattr(bt, "now_at", lambda: AT, raising=False)
    monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
    asked = []

    def query_range(url, q, lookback, step=bt.DEFAULT_STEP):
        asked.append(q)
        return [{"values": [[0, "50"], [1, "2000"]]}] if has_data(q) else []
    monkeypatch.setattr(bt, "query_range", query_range)
    cli_argv("backtest_threshold", "--config-dir", str(cur), "--baseline", str(base),
             "--prometheus", "http://prom:9090", *extra)
    with pytest.raises(SystemExit) as ei:
        bt.main()
    return ei.value.code, capsys.readouterr(), asked


def test_window_reaches_json_text_and_markdown(tmp_path: Path, monkeypatch, capsys,
                                               cli_argv) -> None:
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1000", "5")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1001", "99999")})
    md = tmp_path / "out.md"
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    assert [r.get("window") for r in json.loads(out.out)["changes"]] == [["01:00-02:00"], ["20:00-21:00"]]
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert "1000 -> 1001 (01:00-02:00 UTC)" in out.out
    assert "5 -> 99999 (20:00-21:00 UTC)" in out.out
    assert "`mysql_connections` (20:00-21:00 UTC)" in md.read_text(encoding="utf-8")
    b = _tree(tmp_path / "base2", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1000", "1000")})
    c = _tree(tmp_path / "cur2", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("2000", "2000")})
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert "1000 -> 2000 (01:00-02:00, 20:00-21:00 UTC)" in out.out
    assert "`mysql_connections` (01:00-02:00, 20:00-21:00 UTC)" in md.read_text(encoding="utf-8")


def _map_with(monkeypatch, **entries: dict) -> None:
    """The real observed-map plus these made-up entries (#3017: a key no
    shipped alert compares is not backtested, so a made-up key gets one)."""
    keys = {**bt._observed_map(), **entries}
    monkeypatch.setattr(bt, "_observed_map", lambda: keys)


def _upper(series: str) -> dict:
    return {"scope": "tenant", "direction": ">", "observed_series": series}


def test_dimensioned_keys_are_listed_not_backtested(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """A dimensioned key (served-values' `dimensions`, #2750) is not narrowed
    by the selector asked: listed, marked, never queried, and counted in the
    text report. A `_critical` key declared in defaults with no base key is
    served as a warning row of metric `<x>_critical`, which no threshold rule
    selects (#3017 review): not backtested either, even with a map entry
    for the key."""
    _map_with(monkeypatch, other_critical=_upper("tenant:other_critical:max"))
    def files(v: int) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS + "  other_critical: 80\n",
                "tx.yaml": ("tenants:\n  tx:\n    mysql_connections: 50\n"
                            f"    other_critical: {v}\n"
                            f"    'mysql_connections{{db=\"a\"}}': {v}\n")}
    b, c = _tree(tmp_path / "base", files(90)), _tree(tmp_path / "cur", files(95))
    assert tv.load_served_values(c, at=AT)["tx"].severities["other_critical"] == "warning"
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = {r["metric"]: r for r in json.loads(out.out)["changes"]}
    assert set(rows) == {"other_critical", 'mysql_connections{db="a"}'}, rows
    assert rows['mysql_connections{db="a"}']["backtest"] == "skipped: dimensioned key"
    assert rows['mysql_connections{db="a"}']["status"] == "not_backtested"
    assert rows["other_critical"]["status"] == "not_backtested"
    assert rows["other_critical"]["backtest"] == _NO_BASE.format("other_critical")
    assert asked == []
    md = tmp_path / "out.md"
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert "Not backtested: 1 (dimensioned key), 1 (" + _NO_BASE.format("other_critical")[9:] + ")" in out.out
    text = md.read_text(encoding="utf-8")
    assert "**Not backtested:** 1 (" in text and "1 (dimensioned key)" in text
    # #3002 B: a change no query was sent for has "—" in the Query column.
    assert "| Not backtested: dimensioned key | — |" in text


_NO_BASE = ("skipped: served as a non-critical row of {} (its base is not declared in "
            "the root defaults:), which no alert compares")


def test_a_critical_key_without_its_base_is_not_backtested(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """`mysql_connections_critical` with no `mysql_connections` in the root
    `defaults:` is served as a warning row of metric `connections_critical`
    (da-guard warns it skips the critical row): the `_critical` alert selects
    metric="connections", severity="critical", so nothing compares it — it
    used to be replayed against the critical alert's series (HIGH, rc 1)."""
    def files(v: str) -> dict[str, str]:
        return {"_defaults.yaml": "defaults:\n  mysql_connections_critical: 95\n",
                "tx.yaml": f"tenants:\n  tx:\n    mysql_connections_critical: \"{v}\"\n"}
    b, c = _tree(tmp_path / "base", files("90")), _tree(tmp_path / "cur", files("50"))
    served = tv.load_served_values(c, at=AT)["tx"]
    assert served.series["mysql_connections_critical"][0].labels["severity"] == "warning"
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r["backtest"]) for r in rows] == [
        ("mysql_connections_critical", "not_backtested", _NO_BASE.format("mysql_connections_critical"))]
    assert (rc, asked) == (0, [])


def test_a_severity_no_rule_selects_on_both_sides_is_no_change(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """#3071: `70:high` -> `60:high` is two unreadable values; the exporter
    serves the platform default on both sides, so nothing changed and
    nothing is queried. Before #3071 the exporter served severity="high",
    which no threshold rule selects, and the change was listed as not
    backtested ("selected by no threshold rule", #3017 review)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70:high")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("60:high")})
    assert _backtest(monkeypatch, b, c) == []
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    assert (rc, asked, json.loads(out.out)["changes"]) == (0, [], [])


def test_a_capitalised_severity_is_backtested_on_its_alert(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """#3071: `70:Critical` is read as critical (the suffix is lower-cased),
    so `70:Critical` -> `60:Critical` is compared by the critical alert.
    Before #3071 the exporter served severity="Critical", which the rules
    (selecting "critical" exactly) did not compare."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70:Critical")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("60:Critical")})
    assert {s.labels["severity"] for s in _backtest(monkeypatch, b, c)[0]["series"]} == {"critical"}
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"]) for r in rows] == [("mysql_connections_critical", "analyzed")]
    assert asked == ['tenant:mysql_threads_connected:max{tenant="tx"}']


@pytest.mark.parametrize("side", ["baseline", "current"])
def test_a_sibling_in_one_tree_only_shares_the_threshold(
        tmp_path: Path, monkeypatch, capsys, cli_argv, side: str) -> None:
    """A dimensioned row served in either tree shares the threshold on that
    side, so the base change is not what the alert compares there."""
    def files(v: str, dim: bool) -> dict[str, str]:
        extra = "    'mysql_connections{db=\"a\"}': \"200\"\n" if dim else ""
        return {"_defaults.yaml": _DEFAULTS,
                "tx.yaml": f"tenants:\n  tx:\n    mysql_connections: \"{v}\"\n" + extra}
    b = _tree(tmp_path / "base", files("70", side == "baseline"))
    c = _tree(tmp_path / "cur", files("85", side == "current"))
    changes = [ch for ch in _backtest(monkeypatch, b, c) if ch["metric"] == "mysql_connections"]
    assert [ch.get("dimensioned_siblings") for ch in changes] == [['mysql_connections{db="a"}']]
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = {r["metric"]: r for r in json.loads(out.out)["changes"]}
    assert rows["mysql_connections"]["status"] == "not_backtested"
    assert rows["mysql_connections"]["backtest"].startswith(
        "skipped: dimensioned rows share this alert's threshold")
    assert (rc, asked) == (0, [])


def test_dimensioned_siblings_share_the_threshold(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """The alert's threshold is `max by(tenant)` over the base row and the
    dimensioned rows of the same metric and severity: a base change beside a
    higher `{db="a"}` row is not what the alert compares, so it is not
    backtested (#3017 review). A dimensioned row of the OTHER severity does
    not share it (control: the base change is analyzed)."""
    def files(v: str, dim: str) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS,
                "tx.yaml": (f"tenants:\n  tx:\n    mysql_connections: \"{v}\"\n"
                            f"    'mysql_connections{{db=\"a\"}}': \"{dim}\"\n")}
    b, c = _tree(tmp_path / "base", files("70", "200")), _tree(tmp_path / "cur", files("85", "200"))
    changes = _backtest(monkeypatch, b, c)
    assert [(ch["metric"], ch.get("dimensioned_siblings")) for ch in changes] == [
        ("mysql_connections", ['mysql_connections{db="a"}'])]
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r["backtest"]) for r in rows] == [(
        "mysql_connections", "not_backtested",
        "skipped: dimensioned rows share this alert's threshold (max by tenant): "
        'mysql_connections{db="a"}')]
    assert (rc, asked) == (0, [])
    b = _tree(tmp_path / "base2", files("70", "200:critical"))
    c = _tree(tmp_path / "cur2", files("85", "200:critical"))
    changes = _backtest(monkeypatch, b, c)
    assert [(ch["metric"], ch.get("dimensioned_siblings")) for ch in changes] == [
        ("mysql_connections", None)]
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    assert [r["status"] for r in json.loads(out.out)["changes"]] == ["analyzed"]
    assert asked == ['tenant:mysql_threads_connected:max{tenant="tx"}']


def test_a_key_served_as_critical_is_backtested_on_its_critical_alert(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """`<base>_critical` whose base the root `defaults:` declares is a critical row of the
    base's metric (Go's verdict): served-values says so (#2750 — its series'
    `metric_key` is the base, severity critical), so the alert comparing it
    is `mysql_connections_critical`'s (#3017) and Prometheus is asked, once,
    about the series that alert compares (the observed-map's)."""
    def files(v: int) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS,
                "tx.yaml": ("tenants:\n  tx:\n    mysql_connections: \"70\"\n"
                            f"    mysql_connections_critical: \"{v}\"\n")}
    b, c = _tree(tmp_path / "base", files(99)), _tree(tmp_path / "cur", files(95))
    served = tv.load_served_values(c, at=AT)["tx"]
    assert served.severities["mysql_connections_critical"] == "critical"
    assert served.series["mysql_connections_critical"] == (_ident("mysql_connections", "critical"),)
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r.get("backtest")) for r in rows] == [
        ("mysql_connections_critical", "analyzed", None)]
    # The stub serves 50 then 2000: 99 and 95 both fire once.
    assert (rows[0]["old_breach_count"], rows[0]["new_breach_count"]) == (1, 1)
    assert asked == ['tenant:mysql_threads_connected:max{tenant="tx"}'] == [rows[0]["query"]]
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c)
    assert "Not backtested" not in out.out


def test_a_window_served_as_critical_is_backtested(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """A base key a window overrides to `N:critical` (#2750's other shape): the
    changed pair holds in the critical window only; the series of that window
    is the base's critical row, so it is replayed as the `_critical` alert
    compares it (#3017)."""
    def files(v: str) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
            "tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n      overrides:\n"
            f"        - window: \"01:00-09:00\"\n          value: \"{v}:critical\"\n")}
    b, c = _tree(tmp_path / "base", files("1000")), _tree(tmp_path / "cur", files("1500"))
    changes = _backtest(monkeypatch, b, c)
    assert [(ch["window"], ch["series"]) for ch in changes] == [
        (["01:00-09:00"], [_ident("mysql_connections", "critical")])]
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r["window"]) for r in rows] == [
        ("mysql_connections_critical", "analyzed", ["01:00-09:00"])]
    assert asked == ['tenant:mysql_threads_connected:max{tenant="tx"}']


def test_a_window_served_as_critical_with_no_critical_alert_is_not_backtested(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """mongodb's threshold recording rules normalize severity="warning" only:
    a window overridden to `N:critical` is compared by no alert (#3017), so
    it is listed as not backtested and nothing is asked; the same key's
    warning change is backtested (control)."""
    def files(v: str) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS + "  mongodb_connections_current: 100\n", "tx.yaml": (
            "tenants:\n  tx:\n    mongodb_connections_current:\n      default: \"70\"\n"
            f"      overrides:\n        - window: \"01:00-09:00\"\n          value: \"{v}\"\n")}
    b, c = _tree(tmp_path / "base", files("1000:critical")), _tree(tmp_path / "cur", files("1500:critical"))
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r["backtest"]) for r in rows] == [(
        "mongodb_connections_current_critical", "not_backtested",
        "skipped: no shipped alert compares this key (not in metric_observed_map.yaml)")]
    assert asked == []
    b, c = _tree(tmp_path / "base2", files("1000")), _tree(tmp_path / "cur2", files("1500"))
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    assert [r["status"] for r in json.loads(out.out)["changes"]] == ["analyzed"]
    assert asked == ['tenant:mongodb_connections_current:max{tenant="tx"}']


@pytest.fixture
def x_mapped(monkeypatch):
    _map_with(monkeypatch, x=_upper("tenant:x:max"), x_critical=_upper("tenant:x:max"))


@pytest.mark.usefixtures("x_mapped")
@pytest.mark.parametrize("metric,series,want", [
    # The reason is served-values' fields, never the key's name.
    ("plain", [tv.Series("user_threshold", {}, "plain", False, {"db": "a"}, {})], "dimensioned key"),
    ("plain", [tv.Series("user_threshold", {}, "plain", False, {}, {"ts": "SYS.*"})], "dimensioned key"),
    ('x{db="a"}', [tv.Series("user_threshold", {"severity": "warning"}, "x", False, {}, {})], None),
    # #3017: a row of a severity no threshold rule selects (`60:high`).
    ("x", [tv.Series("user_threshold", {"severity": "high"}, "x", False, {}, {})],
     "severity 'high' is selected by no threshold rule"),
    ("x_critical", [tv.Series("user_threshold", {"severity": "critical"}, "x", False, {}, {})], None),
    # Nothing to ask Prometheus about is not "no data" (fail closed, #2750 N2).
    ("x", [], "served-values gave no non-twin /metrics series"),
    ("x", [tv.Series("user_threshold", {}, "x_old", True, {}, {})],
     "served-values gave no non-twin /metrics series"),
])
def test_not_backtestable_reads_the_series_fields(metric: str, series: list, want: str | None) -> None:
    change = {"tenant": "tx", "metric": metric, "old_value": "1", "new_value": "2", "series": series}
    assert bt.not_backtestable(change) == want


def test_a_change_with_nothing_to_query_is_not_reported_as_no_data(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """A change whose series are only a legacy twin (or none) asks Prometheus
    nothing; the report says so, it does not say "No historical data found"
    (#2750 N2)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("60")})
    twin_only = [_ident("mysql_cpu", "warning", metric="cpu", twin=True)]
    monkeypatch.setattr(bt, "extract_changes_from_dirs", lambda cur, base: [
        {"tenant": "tx", "metric": "k", "old_value": "50", "new_value": "60", "series": twin_only}])
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r["backtest"]) for r in rows] == [
        ("k", "not_backtested", "skipped: served-values gave no non-twin /metrics series")]
    assert asked == []


def test_alert_keys_read_the_series_never_the_twin() -> None:
    """A critical row is compared by `<metric_key>_critical`'s alert, any other
    by `metric_key`'s; an alias target's legacy twin is never read (#2750
    review F1). A key as written (`--tenant`) is canonicalized and loses its
    dimension part (#3017); one no shipped alert compares is no threshold
    (#3111 (e))."""
    change = {"tenant": "tx", "metric": "mysql_threads_running_critical", "series": [
        tv.Series("user_threshold", {"severity": "critical"}, "mysql_threads_running", False, {}, {}),
        tv.Series("user_threshold", {"severity": "critical"}, "mysql_cpu", True, {}, {})]}
    assert bt._alert_keys(change) == (["mysql_threads_running_critical"], None)

    def written(metric: str) -> dict:
        return {"tenant": "tx", "metric": metric, "old_value": "1", "new_value": "2"}
    assert bt._alert_keys(written("mysql_connections")) == (["mysql_connections"], None)
    assert bt._alert_keys(written("mysql_cpu")) == (["mysql_threads_running"], None)
    assert bt._alert_keys(written('mysql_connections{db="a"}')) == (["mysql_connections"], None)
    assert bt._alert_keys(written("k")) == (
        None, "no shipped alert compares this key (not in metric_observed_map.yaml)")


def test_a_retired_name_with_data_is_not_backtested_against(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """`mysql_cpu` 40 -> 90 (the retired spelling of mysql_threads_running) with
    Prometheus holding data under `mysql_cpu` only: the twin's name is not
    this threshold's data (#1231 renamed it because it reads as host CPU%), so
    the change is no_data and nothing is asked about `mysql_cpu` (#2750 F1)."""
    def files(v: int) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS + "  mysql_threads_running: 30\n",
                "tx.yaml": f"tenants:\n  tx:\n    mysql_cpu: \"{v}\"\n"}
    b, c = _tree(tmp_path / "base", files(40)), _tree(tmp_path / "cur", files(90))
    changes = _backtest(monkeypatch, b, c)
    assert [(ch["metric"], ch["series"]) for ch in changes] == [
        ("mysql_threads_running",
         [_ident("mysql_threads_running", "warning", metric="threads_running")])]
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json",
                              has_data=lambda q: q.startswith("mysql_cpu"))
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"]) for r in rows] == [("mysql_threads_running", "no_data")]
    assert asked == ['tenant:mysql_threads_running:avg1m{tenant="tx"}'], asked


@pytest.mark.parametrize("side", ["baseline", "current", "both"])
def test_a_tree_da_guard_refuses_exits_2(tmp_path: Path, side: str) -> None:
    """A file the exporter cannot decode, on either side: exit 2 with the
    `caller_error` envelope, even under --skip-if-unavailable (the trees are
    read before Prometheus is asked)."""
    good = {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")}
    bad = {"_defaults.yaml": _DEFAULTS, "tx.yaml": "tenants:\n  tx: [unclosed\n"}
    b = _tree(tmp_path / "base", bad if side in ("baseline", "both") else good)
    c = _tree(tmp_path / "cur", bad if side in ("current", "both") else good)
    p = subprocess.run(
        [sys.executable, str(BACKTEST), "--config-dir", str(c), "--baseline", str(b),
         "--prometheus", "http://127.0.0.1:9", "--skip-if-unavailable", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 2, (p.stdout, p.stderr)
    doc = json.loads(p.stdout)
    assert (doc["status"], doc["changes"]) == ("caller_error", [])
    if side == "baseline":  # read by da-guard only (the recipe scan reads --config-dir)
        assert doc["reason"] == "served_values_unavailable"
    assert "ERROR: cannot read" in p.stderr and "tx.yaml" in p.stderr


def test_a_tenant_id_promql_cannot_take_is_listed_not_queried(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """#3111 review: the exporter serves a `tenants:` key such as `a|b"c`; built
    into `{tenant="..."}` it made invalid PromQL (rc 2 `query_error`) and broke
    the Markdown row. Now: not backtested, nothing asked, the cell escaped."""
    weird = 'a|b"`c'
    def files(v: str) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS,
                "x.yaml": f"tenants:\n  'a|b\"`c':\n    mysql_connections: \"{v}\"\n"}
    b, c = _tree(tmp_path / "base", files("70")), _tree(tmp_path / "cur", files("85"))
    md = tmp_path / "r.md"
    rc, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert (rc, asked) == (0, [])
    assert "Not backtested: tenant id holds a character" in out.out
    row = next(line for line in md.read_text(encoding="utf-8").splitlines() if "a\\|b" in line)
    assert len(re.findall(r"(?<!\\)\|", row)) == 8  # 7 columns
    assert weird not in md.read_text(encoding="utf-8")


def test_a_metric_cell_from_the_tree_is_escaped(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """#3111 review: a dimensioned key holding `|` broke its Markdown row."""
    def files(v: str) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS,
                "tx.yaml": f"tenants:\n  tx:\n    'mysql_connections{{db=~\"a|b\"}}': \"{v}\"\n"}
    b, c = _tree(tmp_path / "base", files("70")), _tree(tmp_path / "cur", files("85"))
    md = tmp_path / "r.md"
    _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    row = next(line for line in md.read_text(encoding="utf-8").splitlines() if "db=~" in line)
    assert "a\\|b" in row
    assert len(re.findall(r"(?<!\\)\|", row)) == 8  # 7 columns


def test_a_query_cell_holding_a_tenant_id_is_escaped(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """#3111 round 2: a tenant id PromQL takes (`a|b`c`) is put in the query, and
    the Query cell carried it raw — the `|` split the row."""
    def files(v: str) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS,
                "x.yaml": f"tenants:\n  'a|b`c':\n    mysql_connections: \"{v}\"\n"}
    b, c = _tree(tmp_path / "base", files("70")), _tree(tmp_path / "cur", files("85"))
    md = tmp_path / "r.md"
    _, _out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert asked == ['tenant:mysql_threads_connected:max{tenant="a|b`c"}']
    row = next(line for line in md.read_text(encoding="utf-8").splitlines() if "threads" in line)
    assert len(re.findall(r"(?<!\\)\|", row)) == 8, row  # 7 columns
    assert '``tenant:mysql_threads_connected:max{tenant="a\\|b`c"}``' in row, row
