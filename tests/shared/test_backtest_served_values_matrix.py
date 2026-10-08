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
其他時段不同的 key 另有一格釘死數字與 `window`。da-guard 拒收的樹走 CLI，要 exit 2。

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
    ("scheduled-at-instant",
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("1000")},
     {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("75")}),
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
    assert got["scheduled-at-instant"][0]["old_value"] == "70"


@pytest.mark.parametrize("name,base,cur", SHAPES, ids=[n for n, _, _ in SHAPES])
def test_backtest_agrees_with_the_exporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           name: str, base: dict, cur: dict) -> None:
    b, c = _tree(tmp_path / "base", base), _tree(tmp_path / "cur", cur)
    assert _backtest(monkeypatch, b, c) == _oracle(b, c), name


def test_a_change_outside_the_instant_is_named_with_its_window(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Equal at AT (12:00 UTC), different 01:00-09:00: still a change, carrying
    the values of that part of the day and naming it."""
    b = _tree(tmp_path / "base", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("1000")})
    c = _tree(tmp_path / "cur", {"_defaults.yaml": _DEFAULTS, "tx.yaml": _scheduled("2000")})
    assert _oracle(b, c) == []  # the instant alone would miss it
    assert _backtest(monkeypatch, b, c) == [{
        "tenant": "tx", "metric": "mysql_connections",
        "old_value": "1000", "new_value": "2000", "window": "01:00-09:00"}]


@pytest.mark.parametrize("side", ["baseline", "current"])
def test_a_tree_da_guard_refuses_exits_2(tmp_path: Path, side: str) -> None:
    """A file the exporter cannot decode, on either side: exit 2 with the
    `caller_error` envelope, even under --skip-if-unavailable (the trees are
    read before Prometheus is asked)."""
    good = {"_defaults.yaml": _DEFAULTS, "tx.yaml": _wrapper("50")}
    bad = {"_defaults.yaml": _DEFAULTS, "tx.yaml": "tenants:\n  tx: [unclosed\n"}
    b = _tree(tmp_path / "base", bad if side == "baseline" else good)
    c = _tree(tmp_path / "cur", bad if side == "current" else good)
    p = subprocess.run(
        [sys.executable, str(BACKTEST), "--config-dir", str(c), "--baseline", str(b),
         "--prometheus", "http://127.0.0.1:9", "--skip-if-unavailable", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert json.loads(p.stdout)["status"] == "caller_error"
    assert "ERROR: cannot read" in p.stderr and "tx.yaml" in p.stderr
