"""#3489：共用鍵判決移植到 Go（`da-guard key-verdict`，`pkg/keyverdict`），
本檔釘住 Go == Python，比的是整份判決：每個租戶每個鍵的判決（layer、file、
read_by、verdict、severity、reason、disable、served_family、value、served）、
findings（分組、disable_partial、profile 層）、`undecidable` 與 `key_checks`。

比對的不是另寫的一份 fixture，而是每個呼叫 Python 判決的測試所建的樹：在子
pytest 裡照常執行 `_SOURCES` 的測試，外掛 `_key_verdict_parity_plugin` 包住
`_lib_key_verdict.judge_tree`——判決自己的測試、經 validate-config、
explain-keys、產生器 `--validate` 與產生器容器的呼叫都算——每次成功回來時，就
在同一棵樹、同一個時刻（`at`）、同樣的 `--tenant` 跑 `da-guard key-verdict`，
兩份文件經 JSON 正規化後必須相等。兩邊不同就是紅燈，不放寬：先查哪一邊才對。

呼叫端另傳樹裡沒有的 `rules=`，或 `disable_effects=False`，Go 重現不了：那一次
改由 Python 不帶它們重判同一棵樹來比。

比不到的範圍（照實寫出，不是「量了沒事」）：
- 另以子程序執行的 CLI 不在同一個行程，看不到。
- Python 判決丟例外的樹不比：無效的 policy 規則（`InvalidPolicyRules`；Go 刻意
  寬鬆，讀得到的 target 照算，見 `key_verdict.go` 的 `policyHeadsOf`），以及根
  `_defaults.yaml` 讀不進來（Python 停下；Go 以 exit 3 照樣判，根平台檔與
  profiles 取自 parse-tree 讀到的那份）。這兩種差異由 #3489 PR-b 換呼叫端時定案。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Every test module that calls the Python verdict in process.
_SOURCES = (
    "tests/shared/test_key_verdict.py",
    "tests/shared/test_membership_matrix_verdict.py",
    "tests/ops/test_validate_config_unread_keys.py",
    "tests/ops/test_explain_keys.py",
    "tests/ops/test_grar_key_verdict.py",
    "tests/ops/test_gsvc_key_verdict.py",
)

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _nested_env(log: Path) -> dict[str, str]:
    """The nested pytest's environment.

    The nested run is a check, not a measurement: the source tests are
    measured where the suite runs them. ⛔ Without stripping coverage's
    child wiring (tests/conftest.py exports COVERAGE_PROCESS_CONFIG; the
    nested pytest never starts coverage, so it never cleans it up for its
    own subprocesses — #3414's shape) every process the nested run starts
    writes a data file into the parent's directory (57 on one run), and the
    parent's combine fails on one it cannot write (CI: "attempt to write a
    readonly database"). The parent's PYTEST_* (xdist worker ids) are not
    the nested run's either."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("COVERAGE_", "COV_CORE_", "PYTEST_"))}
    env.update(KEY_VERDICT_PARITY_LOG=str(log),
               PYTHONPATH=os.pathsep.join([str(REPO / "tests" / "shared"), os.environ.get("PYTHONPATH", "")]))
    return env


def test_the_nested_run_carries_no_coverage_wiring(monkeypatch, tmp_path):
    monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", "{}")
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage"))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    env = _nested_env(tmp_path / "log")
    assert not [k for k in env if k.startswith(("COVERAGE_", "COV_CORE_", "PYTEST_"))]
    assert env["KEY_VERDICT_PARITY_LOG"] == str(tmp_path / "log")


def test_go_verdict_equals_python_on_every_tree_the_callers_build(tmp_path):
    log = tmp_path / "parity.jsonl"
    env = _nested_env(log)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *_SOURCES, "-q", "-n", "4", "-p", "no:cacheprovider",
         "-p", "_key_verdict_parity_plugin"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", check=False, timeout=900)
    assert proc.returncode == 0, f"the source tests failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}"
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    # An empty comparison is always green: the shape this repo has caught again and again.
    assert len(entries) >= 78, f"only {len(entries)} judge_tree calls compared"
    bad = [e for e in entries if not e["ok"]]
    assert not bad, f"{len(bad)} of {len(entries)} differ; first:\n{bad[0]['conf_d']}\n{bad[0]['diff']}"
