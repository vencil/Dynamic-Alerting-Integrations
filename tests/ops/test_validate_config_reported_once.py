"""validate-config 對同一件事只報一次（#3996）。

共用鍵判決有四種事實與 da-guard 主閘門的 finding 相同：子目錄 `_defaults.yaml` 的保留鍵、
子目錄送不出去的鍵（含退役鍵）、根 `defaults:` 底下的 `_` 鍵。判決在 Go 讀主閘門那個集合時
標上 `gate_kind`，`routes` 列略過這些，由 `guard_errors` 列帶修法句報一次；三種裡有兩種
兩邊的名稱不同，所以不能靠名稱配對。`routes` 列的 `FAIL:` 摘要只計數，不再重列明細。
租戶 id 是 fixture 名（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("da_guard_env", "da_read_env", "da_crdecode_env")

_REPO = Path(__file__).resolve().parents[2]
_ROOT = "defaults:\n  mysql_connection_usage_percent: 80\n"
_TENANT = 'tenants:\n  ta:\n    mysql_connection_usage_percent: "70"\n'

#: (case, files, the key, the gate's finding kind)
_CASES = [
    ("subtree-reserved", {"_defaults.yaml": _ROOT, "team/_defaults.yaml": "_silent_mode: warning\n",
                          "team/ta.yaml": _TENANT}, "_silent_mode", "subtree_default_reserved_key"),
    ("subtree-undeliverable", {"_defaults.yaml": _ROOT, "team/ta.yaml": _TENANT,
                               "team/_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n"},
     "mysql_threads_running", "subtree_default_undeliverable"),
    ("subtree-retired", {"_defaults.yaml": _ROOT, "team/ta.yaml": _TENANT,
                         "team/_defaults.yaml": "defaults:\n  mysql_connections: 30\n"},
     "mysql_connections", "subtree_default_undeliverable"),
    ("root-reserved", {"_defaults.yaml": _ROOT + "  _silent_mode: warning\n", "ta.yaml": _TENANT},
     "_silent_mode", "root_defaults_reserved_key"),
]


def _rows(tmp_path: Path, files: dict[str, str]) -> dict[str, dict]:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    p = subprocess.run([sys.executable, str(_REPO / "scripts/tools/ops/validate_config.py"),
                        "--config-dir", str(tmp_path), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=300)
    return {r["check"]: r for r in json.loads(p.stdout)}


@pytest.mark.parametrize("case,files,key,gate_kind", _CASES, ids=[c[0] for c in _CASES])
def test_a_fact_the_gate_reports_is_reported_once(tmp_path, case, files, key, gate_kind):
    rows = _rows(tmp_path, files)
    guard = [d for d in rows["guard_errors"]["details"] if gate_kind in d and f"`{key}`" in d]
    routes = [d for d in rows["routes"]["details"] if f"`{key}`" in d]
    assert rows["guard_errors"]["status"] == "fail" and len(guard) == 1, rows["guard_errors"]
    assert routes == [], rows["routes"]


def test_a_routes_error_is_listed_once(tmp_path):
    """The summary counts the errors the stream already printed: a key the
    gate does not report (a misspelling) is one line in the row, not two."""
    rows = _rows(tmp_path, {"_defaults.yaml": _ROOT,
                            "ta.yaml": _TENANT + '    mysql_conections: "70"\n'})
    lines = [d for d in rows["routes"]["details"] if "`mysql_conections`" in d]
    assert rows["routes"]["status"] == "fail" and len(lines) == 1, rows["routes"]
    assert any(d.strip() == "FAIL: 1 error(s) found (listed above)" for d in rows["routes"]["details"]), rows["routes"]
