"""tests/conftest.py 的 subprocess coverage 接線 (TRK-379 / #1746)。

⛔ 接線的理由與機制**只寫在 tests/conftest.py 的註解裡**，這裡不複製。

⚠️ 本檔每一格都配一個**反事實**：只證明「設了環境變數就量得到」不算數——那句話對一個
無條件量測所有東西的實作也成立。要證明的是**那個環境變數就是差別所在**。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"
_TOOL = _REPO_ROOT / "scripts" / "tools" / "dx" / "list_subprocess_only_modules.py"

_ENV_KEYS = ("COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START", "COVERAGE_FILE")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakePluginManager:
    def __init__(self, cov):
        self._cov = cov

    def get_plugin(self, name):
        return self._cov if name == "_cov" else None


class _FakeConfig:
    def __init__(self, cov):
        self.pluginmanager = _FakePluginManager(cov)


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return os.environ


def _conftest():
    # ⚠️ 直接以檔案路徑載入，不 `import conftest`：pytest 自己已經載過一份，重複匯入
    # 會拿到那一份而不是這棵樹上的檔案。
    return _load(_CONFTEST, "_wiring_conftest_under_test")


def test_the_wiring_exports_a_child_config_when_coverage_is_on(clean_env) -> None:
    _conftest().pytest_configure(_FakeConfig(cov=object()))
    assert clean_env.get("COVERAGE_PROCESS_CONFIG"), "沒有匯出子行程設定"
    assert clean_env.get("COVERAGE_FILE"), "沒有釘住絕對路徑的資料檔"
    assert os.path.isabs(clean_env["COVERAGE_FILE"]), (
        f"COVERAGE_FILE 不是絕對路徑：{clean_env['COVERAGE_FILE']}——"
        "相對路徑會讓帶 cwd= 的子行程把資料寫進自己的暫存目錄然後被刪掉"
    )


def test_the_wiring_is_a_no_op_without_coverage(clean_env) -> None:
    """⛔ 反事實：沒開 coverage 就不該動環境變數。

    無條件設定會讓每一次普通測試 run 都生出 `.coverage.*` 垃圾檔，而且那些檔案是
    **沒人會去 combine 的**——它們只會累積。
    """
    _conftest().pytest_configure(_FakeConfig(cov=None))
    for key in _ENV_KEYS:
        assert key not in clean_env, f"沒開 coverage 卻設了 {key}"


def test_the_child_config_carries_the_context_the_tool_reads(clean_env) -> None:
    """⛔ conftest 與工具各有一份 `"subprocess"` 字串；這一格是唯一把它們綁住的東西。

    漂掉的後果不是靜默分錯桶（工具會在「未知 context」走 rc 2），但那是一個**全紅**
    而不是一個好懂的訊息。這格讓漂移在它發生的那一刻就被指名。
    """
    import coverage

    conftest = _conftest()
    tool = _load(_TOOL, "_wiring_tool_under_test")
    assert conftest.SUBPROCESS_CONTEXT == tool.SUBPROCESS_CONTEXT, (
        f"context 名稱漂了：conftest={conftest.SUBPROCESS_CONTEXT!r} "
        f"tool={tool.SUBPROCESS_CONTEXT!r}"
    )

    conftest.pytest_configure(_FakeConfig(cov=object()))
    # ⚠️ `deserialize` 是 classmethod 且**回傳**新 config，不是 in-place。
    config = coverage.config.CoverageConfig.deserialize(
        clean_env["COVERAGE_PROCESS_CONFIG"]
    )
    assert config.context == tool.SUBPROCESS_CONTEXT
    assert config.parallel is True, (
        "子行程設定沒有 parallel ⇒ 多個子行程會寫同一個資料檔，last writer wins"
    )


def _measure_child(tmp_path: Path, env_extra: dict) -> set:
    """跑一個子行程並回傳它在資料檔裡留下的 context 集合（空集合＝完全沒被量到）。"""
    import coverage

    target = tmp_path / "child_tool.py"
    target.write_text("def work():\n    return 1\n\n\nwork()\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"

    config = coverage.Coverage().config
    config.source = [str(tmp_path)]
    config.parallel = True
    config.data_file = str(data_file)
    config.context = "subprocess"

    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    env.update({k: (config.serialize() if v is True else v)
                for k, v in env_extra.items()})
    proc = subprocess.run([sys.executable, str(target)],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr

    contexts: set = set()
    for path in tmp_path.glob(".coverage*"):
        data = coverage.CoverageData(basename=str(path))
        data.read()
        for measured in data.measured_files():
            if os.path.samefile(measured, target):
                for line_contexts in data.contexts_by_lineno(measured).values():
                    contexts.update(line_contexts)
    return contexts


def test_a_child_with_the_config_is_measured_under_the_subprocess_context(
    tmp_path: Path,
) -> None:
    contexts = _measure_child(tmp_path, {"COVERAGE_PROCESS_CONFIG": True})
    assert contexts == {"subprocess"}, contexts


def test_a_child_without_the_config_is_not_measured_at_all(tmp_path: Path) -> None:
    """⛔ 控制項：同一個子行程、同一支程式，唯一的差別是那個環境變數。

    這一格若也綠（量到了），代表上一格證明的不是接線——那時「新增偵測力」這句話沒有依據。
    """
    contexts = _measure_child(tmp_path, {})
    assert contexts == set(), (
        f"沒有設定卻被量到 {contexts} ⇒ 上一格測到的不是接線的效果"
    )
