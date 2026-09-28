"""analyze-gaps 與 migrate 的輸入：不存在的路徑、找不到的字典（issue 1513）。

兩件事先前都靜默退化：

- analyze-gaps 的 ``--config-dir`` / ``--tenant-config`` 指到不存在的路徑時，
  rc=0 並印「No custom_ metrics found」，與「掃過了、真的沒有」無法區分。
- 兩支工具的預設字典路徑都是「工具自己那一層」。映像把工具與
  ``metric-dictionary.yaml`` 攤平在同一層（build.sh）；repo 的工具在
  ``scripts/tools/ops/``，字典在上一層。照文件跑
  ``python3 scripts/tools/ops/<tool>.py`` 的讀者拿到空字典：analyze-gaps 退化成
  前綴猜測，migrate 的黃金標準比對整個消失。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_OPS = _REPO / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
sys.path.insert(0, str(_OPS.parent))

import migrate_rule  # noqa: E402
from _lib_io import find_metric_dictionary  # noqa: E402

_ANALYZE = _OPS / "analyze_rule_pack_gaps.py"


def _run(script, *args, cwd=None):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True,
                          text=True, encoding="utf-8", timeout=120, cwd=cwd)


@pytest.mark.parametrize("flag", ["--config-dir", "--tenant-config"])
def test_analyze_gaps_missing_input_is_a_caller_error(tmp_path, flag):
    missing = tmp_path / "nope"
    proc = _run(_ANALYZE, flag, str(missing))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert flag in proc.stderr and "nope" in proc.stderr
    assert "No custom_ metrics found" not in proc.stdout


def test_analyze_gaps_explicit_dictionary_that_is_missing_is_a_caller_error(tmp_path):
    tenant = tmp_path / "t.yaml"
    tenant.write_text("tenants:\n  t1:\n    custom_x: '1'\n", encoding="utf-8")
    proc = _run(_ANALYZE, "--tenant-config", str(tenant),
                "--metric-dictionary", str(tmp_path / "nope.yaml"))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "--metric-dictionary" in proc.stderr


def test_analyze_gaps_finds_the_default_dictionary_in_the_repo_layout(tmp_path):
    """正控制：同一個 key，有字典時是 exact，沒字典時只剩 prefix。"""
    tenant = tmp_path / "t.yaml"
    tenant.write_text(
        "tenants:\n  t1:\n    custom_mysql_global_status_threads_connected: '150'\n",
        encoding="utf-8")
    with_dict = _run(_ANALYZE, "--tenant-config", str(tenant), "--json")
    assert with_dict.returncode == 0, with_dict.stderr
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    without = _run(_ANALYZE, "--tenant-config", str(tenant), "--json",
                   "--metric-dictionary", str(empty))
    assert without.returncode == 0, without.stderr
    assert [r["match_type"] for r in json.loads(with_dict.stdout)] == ["exact"]
    assert [r["match_type"] for r in json.loads(without.stdout)] == ["prefix"]


def test_find_metric_dictionary_covers_both_layouts(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "metric-dictionary.yaml").write_text("{}\n", encoding="utf-8")
    assert find_metric_dictionary(str(flat)) == str(flat / "metric-dictionary.yaml")

    nested = tmp_path / "repo" / "ops"
    nested.mkdir(parents=True)
    (tmp_path / "repo" / "metric-dictionary.yaml").write_text("{}\n", encoding="utf-8")
    assert find_metric_dictionary(str(nested)) == str(tmp_path / "repo" / "metric-dictionary.yaml")

    assert find_metric_dictionary(str(tmp_path / "none")) is None


def test_analyze_gaps_warns_when_no_dictionary_is_found(tmp_path):
    """兩個位置都找不到時要出聲：沒有字典時的結果只剩前綴猜測。"""
    lonely = tmp_path / "ops"
    lonely.mkdir()
    for name in os.listdir(_OPS):
        if name.endswith(".py"):
            (lonely / name).write_bytes((_OPS / name).read_bytes())
    for name in os.listdir(_OPS.parent):
        if name.startswith("_lib") and name.endswith(".py"):
            (lonely / name).write_bytes((_OPS.parent / name).read_bytes())
    tenant = tmp_path / "t.yaml"
    tenant.write_text("tenants:\n  t1:\n    custom_x: '1'\n", encoding="utf-8")
    proc = _run(lonely / "analyze_rule_pack_gaps.py", "--tenant-config", str(tenant))
    assert proc.returncode == 0, proc.stderr
    assert "metric-dictionary.yaml" in proc.stderr and "WARN" in proc.stderr


def test_migrate_loads_the_dictionary_in_the_repo_layout():
    assert len(migrate_rule.load_metric_dictionary()) > 0
