"""`backtest --config-dir/--baseline` 找到的門檻變更要與 Go 一致（#2119）：兩棵樹各自由
da-guard served-values（/metrics）讀，逐租戶、逐 key 比 exporter 送出的值。

step 0 的政策：Python 工具不自己解讀 YAML 語意，值一律來自 Go。`extract_changes_from_dirs`
原本用 PyYAML 重讀兩個目錄、比租戶檔的頂層 key。修正前（main 6447be54）實測：

- 只改 `_platform.yaml` 的 `tenants.tx.mysql_connections`（50 → 60）：回 0 個變更；
  exporter 對新樹送 60。
- 租戶檔是 `tenants:` 包裝格式（conf.d 的標準格式）：回 1 個 metric=`tenants`、值是整個
  dict 字串的變更，而不是 `mysql_connections`。
- 平鋪的租戶檔（`mysql_connections: "50"`，沒有 `tenants:`）：回 1 個變更；exporter 不從
  這種檔送任何租戶（#2115 R3），改它 /metrics 不變。

每格比對：`extract_changes_from_dirs` 的答案 == 直接對兩棵樹跑 served-values、在同一個
`at` 逐租戶逐 threshold key 比值得到的答案（一邊沒送的 key 或租戶記 None）。排程只在一天
其他時段不同的 key 另有幾格釘死數字與 `window`（每一段不同都要列出，不只第一段；同一組
新舊值只算一個變更，`window` 列出它成立的所有時段）。
da-guard 拒收的樹、或某時段 /metrics 收不到，都要 exit 2。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
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


def _served_at(conf_d: Path) -> dict[tuple[str, str], str | None]:
    """{(tenant, threshold key): value} as /metrics serves it at AT."""
    return {(t, k): _text(v.values[k])
            for t, v in tv.load_served_values(conf_d, at=AT).items() for k in v.severities}


def _oracle(base: Path, cur: Path) -> list[dict]:
    old, new = _served_at(base), _served_at(cur)
    return [{"tenant": t, "metric": k, "old_value": old.get((t, k)), "new_value": new.get((t, k))}
            for t, k in sorted(set(old) | set(new)) if old.get((t, k)) != new.get((t, k))]


def _backtest(monkeypatch: pytest.MonkeyPatch, base: Path, cur: Path) -> list[dict]:
    monkeypatch.setattr(bt, "now_at", lambda: AT, raising=False)
    return bt.extract_changes_from_dirs(str(cur), str(base))


def test_matrix_is_not_vacuous(tmp_path: Path) -> None:
    """Pinned numbers on the Go side, so equal readings mean something."""
    got = {}
    for name, base, cur in SHAPES:
        got[name] = _oracle(_tree(tmp_path / name / "base", base), _tree(tmp_path / name / "cur", cur))
    assert got["platform-only"] == [{"tenant": "tx", "metric": "mysql_connections",
                                     "old_value": "50", "new_value": "60"}]
    assert got["defaults-layer"] == [{"tenant": "tx", "metric": "mysql_connections",
                                      "old_value": "80", "new_value": "85"}]
    assert got["profile"] == [{"tenant": "tx", "metric": "mysql_slow",
                               "old_value": "60", "new_value": "40"}]
    assert got["flat-file-not-served"] == [] and got["unchanged"] == []
    assert {(c["tenant"], c["old_value"]) for c in got["tenant-added"]} == {("ty", None)}
    assert got["key-disabled"] == [{"tenant": "tx", "metric": "mysql_connections",
                                    "old_value": "50", "new_value": None}]


@pytest.mark.parametrize("name,base,cur", SHAPES, ids=[n for n, _, _ in SHAPES])
def test_backtest_agrees_with_the_exporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           name: str, base: dict, cur: dict) -> None:
    b, c = _tree(tmp_path / "base", base), _tree(tmp_path / "cur", cur)
    assert _backtest(monkeypatch, b, c) == _oracle(b, c), name


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
    assert _backtest(monkeypatch, b, c) == [{
        "tenant": "tx", "metric": "mysql_connections",
        "old_value": "1000", "new_value": "2000", "window": ["01:00-09:00"]}]


def test_every_differing_part_of_the_day_is_a_row(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two overrides both changed: two rows, not the first one only (round-1
    review: the 20:00 change used to be dropped)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1000", "5")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1001", "99999")})
    assert _backtest(monkeypatch, b, c) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "1000", "new_value": "1001",
         "window": ["01:00-02:00"]},
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "5", "new_value": "99999",
         "window": ["20:00-21:00"]}]


def test_one_pair_in_several_parts_of_the_day_is_one_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backtested once over the whole lookback, so counted once (round-2
    review: a 22:00-02:00 override used to make two HIGH rows)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("1000", "1000")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _two_windows("2000", "2000")})
    assert _backtest(monkeypatch, b, c) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "1000", "new_value": "2000",
         "window": ["01:00-02:00", "20:00-21:00"]}]
    over_midnight = ("tenants:\n  tx:\n    mysql_connections:\n      default: \"70\"\n"
                     "      overrides:\n        - window: \"22:00-02:00\"\n          value: \"{v}\"\n")
    b = _tree(tmp_path / "base2", {"_defaults.yaml": _DEFAULTS, "tx.yaml": over_midnight.format(v=1000)})
    c = _tree(tmp_path / "cur2", {"_defaults.yaml": _DEFAULTS, "tx.yaml": over_midnight.format(v=5)})
    assert _backtest(monkeypatch, b, c) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "1000", "new_value": "5",
         "window": ["00:00-02:00", "22:00-24:00"]}]


def test_a_severity_boundary_does_not_split_a_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The current side changes severity at 01:00 and 09:00 but serves 75 all
    day: one change, all day, no window (the run-merge branch)."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("70")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": (
        "tenants:\n  tx:\n    mysql_connections:\n      default: \"75\"\n      overrides:\n"
        "        - window: \"01:00-09:00\"\n          value: \"75:critical\"\n")})
    t = tv.load_served_tree(c, at=AT, schedules=True).tenants["tx"]
    assert len(t.schedules["mysql_connections"].segments) == 3  # not vacuous
    # 01:00-09:00 is served as a critical row, so the change says so.
    assert _backtest(monkeypatch, b, c) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "70", "new_value": "75",
         "severity": "critical"}]


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
    assert _backtest(monkeypatch, b, c) == [
        {"tenant": "tx", "metric": "mysql_connections", "old_value": "70", "new_value": "75",
         "window": ["00:00-01:00", "09:00-24:00"]}]


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
    values = tv.TenantValues("tx", {}, {}, {}, {}, {"k": tv.KeySchedule([seg], None, None)})
    with pytest.raises(tv.ServedValuesError, match="gather failed"):
        bt._served_day(values, "k", "current")


def _run_main(monkeypatch, capsys, cli_argv, base: Path, cur: Path, *extra: str):
    """`main` in-process with Prometheus answered by a stub series (50 then 2000)."""
    monkeypatch.setattr(bt, "now_at", lambda: AT, raising=False)
    monkeypatch.setattr(bt, "prometheus_available", lambda url, timeout=5: True)
    asked = []

    def query_range(url, q, lookback, step=bt.DEFAULT_STEP):
        asked.append(q)
        return [{"values": [[0, "50"], [1, "2000"]]}]
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


def test_dimensioned_keys_are_listed_not_backtested(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """A dimensioned key is no PromQL selector: listed, marked, never queried,
    and counted in the text report. A `_critical` key is an ordinary
    threshold to /metrics (declared in defaults with no base key, it is
    served as a warning row) and is backtested as one (round-2 review)."""
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
    assert rows["other_critical"]["status"] == "analyzed" and "backtest" not in rows["other_critical"]
    assert asked and all("{db=" not in q for q in asked) and any("other_critical" in q for q in asked)
    md = tmp_path / "out.md"
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c, "--markdown-output", str(md))
    assert "Not backtested: 1 (dimensioned key)" in out.out
    assert "**Not backtested:** 1 (dimensioned key)" in md.read_text(encoding="utf-8")


def test_a_key_served_as_critical_is_listed_not_backtested(
        tmp_path: Path, monkeypatch, capsys, cli_argv) -> None:
    """`<base>_critical` whose base /metrics serves is a critical row (Go's
    verdict, round-3 review): no series of its own, so listed and not
    queried. The base-less `*_critical` above is a warning row and is."""
    def files(v: int) -> dict[str, str]:
        return {"_defaults.yaml": _DEFAULTS,
                "tx.yaml": ("tenants:\n  tx:\n    mysql_connections: \"70\"\n"
                            f"    mysql_connections_critical: \"{v}\"\n")}
    b, c = _tree(tmp_path / "base", files(99)), _tree(tmp_path / "cur", files(95))
    assert tv.load_served_values(c, at=AT)["tx"].severities["mysql_connections_critical"] == "critical"
    _, out, asked = _run_main(monkeypatch, capsys, cli_argv, b, c, "--json")
    rows = json.loads(out.out)["changes"]
    assert [(r["metric"], r["status"], r["backtest"]) for r in rows] == [
        ("mysql_connections_critical", "not_backtested", "skipped: critical-severity key")]
    assert asked == []
    _, out, _ = _run_main(monkeypatch, capsys, cli_argv, b, c)
    assert "Not backtested: 1 (critical-severity key)" in out.out


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
