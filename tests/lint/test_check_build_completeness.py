"""Tests for scripts/tools/lint/check_build_completeness.py.

Gap 4 (TRK-007 backlog) — second lint self-test in the chain (after
test_check_metric_dictionary). Auto-hook lint at 133 LOC, previously
zero unit-test coverage. The bidirectional COMMAND_MAP ↔ build.sh
TOOL_FILES sync logic is exactly the kind of multi-branch lint
where a regression silently lets `da-tools <cmd>` crash in the
shipped Docker image (the v2.3.0 opa-evaluate bug this lint exists
to prevent).

Covers:
  - check_bidirectional: clean / missing-in-build error /
    orphan-in-build warning / mixed
  - format_text_report: clean header, error/warning prefixes, count line
  - format_json_report: shape + pass-flag semantics
  - main CLI: missing entrypoint exits 2, missing build.sh exits 2,
    --ci with error exits 1, --ci with warning-only exits 0,
    --json flag, repo-files smoke regression

The check_bidirectional layer is pure (just sets + dict math) so we
test it directly without monkeypatching files.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_build_completeness.py"

# Add the lint dir to sys.path so the script's `from _lint_helpers import …`
# works when we exec it via importlib.
_LINT_DIR = str(REPO_ROOT / "scripts" / "tools" / "lint")
if _LINT_DIR not in sys.path:
    sys.path.insert(0, _LINT_DIR)

_spec = importlib.util.spec_from_file_location("check_build_completeness", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_build_completeness"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# Helpers
# ============================================================


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# Minimal entrypoint.py + build.sh fixtures used by the CLI tests.

_ENTRYPOINT_TEMPLATE = """COMMAND_MAP = {{
{lines}
}}
"""

_BUILD_SH_TEMPLATE = """#!/bin/bash
TOOL_FILES=(
{lines}
)
"""


def _make_entrypoint(tmp_path: Path, mapping: dict[str, str]) -> Path:
    body_lines = "\n".join(
        f'    "{cmd}": "{script}",' for cmd, script in mapping.items()
    )
    f = tmp_path / "entrypoint.py"
    _write(f, _ENTRYPOINT_TEMPLATE.format(lines=body_lines))
    return f


def _make_build_sh(tmp_path: Path, tools: list[str]) -> Path:
    body_lines = "\n".join(f'    "{t}"' for t in tools)
    f = tmp_path / "build.sh"
    _write(f, _BUILD_SH_TEMPLATE.format(lines=body_lines))
    return f


# ============================================================
# check_bidirectional — pure logic
# ============================================================


class TestCheckBidirectional:

    def test_clean_returns_empty(self):
        # Property: when COMMAND_MAP scripts ⊆ build_tools and every
        # build_tools .py has a COMMAND_MAP entry → no errors.
        cm = {"check-alert": "check_alert.py", "diagnose": "diagnose.py"}
        bt = {"check_alert.py", "diagnose.py"}
        assert mod.check_bidirectional(cm, bt) == []

    def test_missing_in_build_is_error(self):
        # Property: COMMAND_MAP entry whose script isn't in build.sh →
        # ERROR (Docker image would crash on `da-tools <cmd>`).
        cm = {"new-cmd": "new_cmd.py"}
        bt = set()
        errors = mod.check_bidirectional(cm, bt)
        assert len(errors) == 1
        severity, msg = errors[0]
        assert severity == "error"
        assert "new_cmd.py" in msg
        assert "new-cmd" in msg
        assert "crash" in msg

    def test_orphan_in_build_is_warning(self):
        # Property: build.sh script that nothing in COMMAND_MAP points to
        # AND that isn't BUILD_EXEMPT → WARNING (shipped but unreachable).
        cm = {}
        bt = {"orphan_tool.py"}
        errors = mod.check_bidirectional(cm, bt)
        assert len(errors) == 1
        severity, msg = errors[0]
        assert severity == "warning"
        assert "orphan_tool.py" in msg

    def test_build_exempt_not_orphan(self):
        # Property: BUILD_EXEMPT items (libraries / data) are NOT flagged
        # as orphans even when not in COMMAND_MAP.
        from _lint_helpers import BUILD_EXEMPT
        cm = {}
        # Pick the first .py exempt entry so we exercise the .py filter +
        # exempt allowlist together.
        exempt_py = next(x for x in BUILD_EXEMPT if x.endswith(".py"))
        bt = {exempt_py}
        assert mod.check_bidirectional(cm, bt) == []

    def test_non_py_in_build_not_orphan(self):
        # Property: non-.py files in TOOL_FILES (e.g. data files) are
        # filtered out before the orphan check (the `.py` suffix gate).
        cm = {}
        bt = {"some-data.yaml", "config.json"}
        assert mod.check_bidirectional(cm, bt) == []

    def test_mixed_errors_and_warnings(self):
        # Property: a missing-in-build error AND an orphan warning can
        # both be surfaced from a single call.
        cm = {"missing": "missing_script.py"}
        bt = {"orphan_script.py"}
        errors = mod.check_bidirectional(cm, bt)
        severities = sorted(s for s, _ in errors)
        assert severities == ["error", "warning"]

    def test_multiple_missing_sorted(self):
        # Property: missing entries are reported in sorted order
        # (deterministic for diffable CI output).
        cm = {"a": "a.py", "b": "b.py", "c": "c.py"}
        bt = set()
        errors = mod.check_bidirectional(cm, bt)
        scripts_in_order = [
            line for s, line in errors if s == "error"
        ]
        # The script name is embedded in the error message; check order.
        a_idx = next(i for i, m in enumerate(scripts_in_order) if "a.py" in m)
        b_idx = next(i for i, m in enumerate(scripts_in_order) if "b.py" in m)
        c_idx = next(i for i, m in enumerate(scripts_in_order) if "c.py" in m)
        assert a_idx < b_idx < c_idx


# ============================================================
# format_text_report
# ============================================================


class TestFormatTextReport:

    def test_clean_report_has_success_marker(self):
        out = mod.format_text_report([], {"a": "a.py"}, {"a.py"})
        assert "✓" in out
        assert "完全一致" in out

    def test_error_report_has_error_prefix(self):
        errors = [("error", "boom")]
        out = mod.format_text_report(errors, {}, set())
        assert "✗ ERROR" in out
        assert "boom" in out
        assert "1 錯誤" in out
        assert "0 警告" in out

    def test_warning_report_has_warning_prefix(self):
        errors = [("warning", "noise")]
        out = mod.format_text_report(errors, {}, {"noise.py"})
        assert "⚠ WARNING" in out
        assert "noise" in out
        assert "1 警告" in out

    def test_counts_in_header(self):
        out = mod.format_text_report(
            [], {"a": "a.py", "b": "b.py"}, {"a.py", "b.py", "c.py"})
        assert "2 命令" in out
        assert "3 檔案" in out


# ============================================================
# format_json_report
# ============================================================


class TestFormatJsonReport:

    def test_clean_payload_passes(self):
        s = mod.format_json_report([], {"a": "a.py"}, {"a.py"})
        payload = json.loads(s)
        assert payload["check"] == "build-completeness"
        assert payload["command_map_count"] == 1
        assert payload["build_tools_count"] == 1
        assert payload["errors"] == []
        assert payload["pass"] is True

    def test_error_payload_does_not_pass(self):
        errors = [("error", "boom"), ("warning", "noise")]
        s = mod.format_json_report(errors, {}, set())
        payload = json.loads(s)
        # `pass` flag tracks errors only, not warnings.
        assert payload["pass"] is False
        assert len(payload["errors"]) == 2
        assert {e["severity"] for e in payload["errors"]} == {"error", "warning"}

    def test_warning_only_payload_passes(self):
        # Property: `pass=True` when there are warnings but no errors.
        errors = [("warning", "noise")]
        s = mod.format_json_report(errors, {}, {"noise.py"})
        payload = json.loads(s)
        assert payload["pass"] is True

    def test_unicode_preserved(self):
        # Property: `ensure_ascii=False` keeps Chinese characters legible
        # in CI logs.
        errors = [("error", "中文錯誤訊息")]
        s = mod.format_json_report(errors, {}, set())
        assert "中文錯誤訊息" in s
        assert "\\u" not in s


# ============================================================
# main — CLI / exit codes
# ============================================================


class TestMainCLI:

    def test_missing_entrypoint_exits_two(
        self, tmp_path, monkeypatch, capsys
    ):
        # Property: missing entrypoint.py is a CONFIG error (exit 2),
        # not a lint failure.
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", tmp_path / "nope.py")
        # build.sh isn't reached.
        monkeypatch.setattr(mod, "BUILD_SH_PATH", tmp_path / "anywhere.sh")
        monkeypatch.setattr(sys, "argv", ["check_build_completeness"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        assert "entrypoint.py" in capsys.readouterr().err

    def test_missing_build_sh_exits_two(
        self, tmp_path, monkeypatch, capsys
    ):
        # Property: missing build.sh also a CONFIG error.
        ep = _make_entrypoint(tmp_path, {"a": "a.py"})
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", ep)
        monkeypatch.setattr(mod, "BUILD_SH_PATH", tmp_path / "nope.sh")
        monkeypatch.setattr(sys, "argv", ["check_build_completeness"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        assert "build.sh" in capsys.readouterr().err

    def test_clean_repo_exits_zero(self, tmp_path, monkeypatch, capsys):
        # Positive: matching entrypoint + build.sh → exit 0.
        ep = _make_entrypoint(tmp_path, {"check-alert": "check_alert.py"})
        bs = _make_build_sh(tmp_path, ["scripts/tools/check_alert.py"])
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", ep)
        monkeypatch.setattr(mod, "BUILD_SH_PATH", bs)
        monkeypatch.setattr(sys, "argv", ["check_build_completeness"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        assert "完全一致" in capsys.readouterr().out

    def test_missing_in_build_exits_one_with_ci(
        self, tmp_path, monkeypatch, capsys
    ):
        # Negative: COMMAND_MAP entry without build.sh entry →
        # `--ci` exits 1 (this is the v2.3.0 opa-evaluate guard).
        ep = _make_entrypoint(tmp_path, {"new-cmd": "new_cmd.py"})
        bs = _make_build_sh(tmp_path, [])
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", ep)
        monkeypatch.setattr(mod, "BUILD_SH_PATH", bs)
        monkeypatch.setattr(sys, "argv", ["check_build_completeness", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "new_cmd.py" in out
        assert "ERROR" in out

    def test_missing_in_build_without_ci_exits_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        # Property: without `--ci`, even errors don't cause exit 1
        # (informational mode).
        ep = _make_entrypoint(tmp_path, {"new-cmd": "new_cmd.py"})
        bs = _make_build_sh(tmp_path, [])
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", ep)
        monkeypatch.setattr(mod, "BUILD_SH_PATH", bs)
        monkeypatch.setattr(sys, "argv", ["check_build_completeness"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0  # report-only without --ci

    def test_orphan_warning_only_exits_zero_under_ci(
        self, tmp_path, monkeypatch, capsys
    ):
        # Property: warning-only state passes `--ci` (only errors fail).
        ep = _make_entrypoint(tmp_path, {})
        bs = _make_build_sh(tmp_path, ["scripts/tools/orphan.py"])
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", ep)
        monkeypatch.setattr(mod, "BUILD_SH_PATH", bs)
        monkeypatch.setattr(sys, "argv", ["check_build_completeness", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "orphan.py" in out

    def test_json_flag_emits_parseable_json(
        self, tmp_path, monkeypatch, capsys
    ):
        ep = _make_entrypoint(tmp_path, {"a": "a.py"})
        bs = _make_build_sh(tmp_path, ["scripts/tools/a.py"])
        monkeypatch.setattr(mod, "ENTRYPOINT_PATH", ep)
        monkeypatch.setattr(mod, "BUILD_SH_PATH", bs)
        monkeypatch.setattr(sys, "argv", ["check_build_completeness", "--json"])
        with pytest.raises(SystemExit):
            mod.main()
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["check"] == "build-completeness"
        assert payload["pass"] is True


# ============================================================
# Repo-level smoke regression guard
# ============================================================


class TestRepoSmoke:

    def test_actual_repo_passes_or_warn_only(self, monkeypatch):
        """The shipped entrypoint.py + build.sh must pass the lint.

        Belt-and-suspenders alongside the pre-commit hook: if a future
        edit breaks the bidirectional sync, this test fails locally
        even before pre-commit fires.
        """
        monkeypatch.setattr(sys, "argv", ["check_build_completeness", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0, (
            "repo's entrypoint.py + build.sh fail their own bidirectional check"
        )


# ============================================================
# check_underscore_imports — transitive sibling-lib packaging guard
# ============================================================
# da-tools image packaging bugfix (PR-0): threshold_recommend.py had a
# top-level `import _observed_map_lib` but build.sh TOOL_FILES omitted the
# lib → ImportError inside the flat image for BOTH threshold-recommend and
# (transitively) threshold-govern. These tests pin the防再犯 lint so a
# future shipped tool that imports a NEW sibling `_xxx.py` without listing
# it in TOOL_FILES fails locally + in CI.


def _make_tool(tools_src: Path, rel: str, body: str) -> None:
    """Write a fake shipped tool at ``tools_src/<rel>`` with ``body``."""
    p = tools_src / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    _write(p, body)


class TestCheckUnderscoreImports:

    def test_missing_sibling_lib_is_error(self, tmp_path):
        # NEGATIVE FIXTURE: a shipped tool imports a real sibling _lib.py
        # that is NOT in TOOL_FILES → ERROR (the _observed_map_lib bug).
        _make_tool(tmp_path, "ops/mytool.py", "import _mylib\n")
        _make_tool(tmp_path, "ops/_mylib.py", "X = 1\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        severity, msg = errors[0]
        assert severity == "error"
        assert "_mylib" in msg
        assert "mytool.py" in msg
        assert "ImportError" in msg

    def test_present_sibling_lib_is_clean(self, tmp_path):
        # POSITIVE: same import but the lib IS listed in TOOL_FILES → clean.
        _make_tool(tmp_path, "ops/mytool.py", "import _mylib\n")
        _make_tool(tmp_path, "ops/_mylib.py", "X = 1\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py", "_mylib.py"}, tools_src=tmp_path)
        assert errors == []

    def test_from_import_form_is_caught(self, tmp_path):
        # Property: `from _mylib import x` form is caught too.
        _make_tool(tmp_path, "ops/mytool.py", "from _mylib import x\n")
        _make_tool(tmp_path, "ops/_mylib.py", "x = 1\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        assert "_mylib" in errors[0][1]

    def test_function_level_import_is_caught(self, tmp_path):
        # Property: an import nested inside a function still crashes at call
        # time in the image, so ast.walk (not just top-level) must catch it.
        _make_tool(
            tmp_path, "ops/mytool.py",
            "def run():\n    import _mylib\n    return _mylib\n")
        _make_tool(tmp_path, "ops/_mylib.py", "X = 1\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        assert "_mylib" in errors[0][1]

    def test_non_repo_underscore_module_skipped(self, tmp_path):
        # Property: an underscore module with NO matching repo file is a
        # stdlib/external import (e.g. `_socket`, `_lib_that_pip_installs`)
        # → NOT flagged (would be a false-positive).
        _make_tool(tmp_path, "ops/mytool.py", "import _not_a_repo_lib\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py"}, tools_src=tmp_path)
        assert errors == []

    def test_dunder_import_skipped(self, tmp_path):
        # Property: `from __future__ import annotations` (dunder) is excluded.
        _make_tool(
            tmp_path, "ops/mytool.py",
            "from __future__ import annotations\nimport os\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py"}, tools_src=tmp_path)
        assert errors == []

    def test_non_py_entry_skipped(self, tmp_path):
        # Property: non-.py TOOL_FILES entries (data files) are skipped by
        # the import scanner (they have no imports to walk).
        _make_tool(tmp_path, "some-data.yaml", "version: 1\n")
        errors = mod.check_underscore_imports(
            {"some-data.yaml"}, set(), tools_src=tmp_path)
        assert errors == []

    def test_missing_source_file_not_double_reported(self, tmp_path):
        # Property: a TOOL_FILES entry whose source file doesn't exist is
        # left to build.sh's own cp existence check — this scanner stays
        # silent (no crash, no error) rather than double-reporting.
        errors = mod.check_underscore_imports(
            {"ops/ghost.py"}, {"ghost.py"}, tools_src=tmp_path)
        assert errors == []

    def test_sibling_in_root_dir_resolves(self, tmp_path):
        # Property: a root-level _lib (scripts/tools/_lib_x.py) imported by
        # an ops/ tool resolves via the root candidate dir.
        _make_tool(tmp_path, "ops/mytool.py", "import _lib_x\n")
        _make_tool(tmp_path, "_lib_x.py", "X = 1\n")
        errors = mod.check_underscore_imports(
            {"ops/mytool.py"}, {"mytool.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        assert "_lib_x" in errors[0][1]

    def test_repo_files_pass_underscore_scan(self):
        # Regression: the ACTUAL shipped TOOL_FILES must satisfy the
        # transitive underscore-import guard (would have failed before the
        # _observed_map_lib fix).
        from _lint_helpers import parse_build_sh_tools, parse_build_sh_tool_paths
        rel_paths = parse_build_sh_tool_paths()
        build_tools = parse_build_sh_tools()
        errors = mod.check_underscore_imports(rel_paths, build_tools)
        assert errors == [], (
            "shipped tools import sibling _libs not listed in TOOL_FILES: "
            + "; ".join(m for _, m in errors)
        )


# ============================================================
# check_required_data_files — module → data-file co-shipping guard
# ============================================================


class TestCheckRequiredDataFiles:

    def test_missing_data_file_is_error(self):
        # NEGATIVE FIXTURE: module shipped but its required data file absent
        # → ERROR (the fail-quiet trap: load returns {} → all keys skipped).
        required = {"_observed_map_lib.py": ("metric_observed_map.yaml",)}
        errors = mod.check_required_data_files(
            {"_observed_map_lib.py"}, required=required)
        assert len(errors) == 1
        severity, msg = errors[0]
        assert severity == "error"
        assert "metric_observed_map.yaml" in msg
        assert "_observed_map_lib.py" in msg

    def test_present_data_file_is_clean(self):
        # POSITIVE: module + data file both shipped → clean.
        required = {"_observed_map_lib.py": ("metric_observed_map.yaml",)}
        errors = mod.check_required_data_files(
            {"_observed_map_lib.py", "metric_observed_map.yaml"},
            required=required)
        assert errors == []

    def test_module_not_shipped_no_requirement(self):
        # Property: if the module itself isn't in TOOL_FILES, its data-file
        # requirement doesn't apply (no spurious error).
        required = {"_observed_map_lib.py": ("metric_observed_map.yaml",)}
        errors = mod.check_required_data_files(set(), required=required)
        assert errors == []

    def test_multiple_data_files_each_checked(self):
        # Property: a module requiring >1 data file reports each missing one.
        required = {"tool.py": ("a.yaml", "b.yaml")}
        errors = mod.check_required_data_files(
            {"tool.py", "a.yaml"}, required=required)
        assert len(errors) == 1
        assert "b.yaml" in errors[0][1]

    def test_repo_required_data_files_present(self):
        # Regression: the real REQUIRED_DATA_FILES mapping must be satisfied
        # by everything that actually ships.
        #
        # ⛔ 「出貨集合」自 #1494 起是**兩條**搬運路徑的聯集：TOOL_FILES
        # （相對 scripts/tools/）與 REPO_DATA_FILES（相對 repo root，給住在
        # scripts/tools/ 之外的資料檔）。這一格原本只餵前者；那個定義在
        # configmap-rules-platform.yaml 進來之後就不再等於「會不會一起進
        # 映像」，會把一個確實有出貨的檔案報成缺漏。
        # 反向控制在 TestRepoDataFilesArePairedWithTheirConsumer，
        # 它斷言只餵 TOOL_FILES 時規則**仍會**開火 —— 所以這裡放寬的是
        # 集合的定義，不是規則的嚴格度。
        from _lint_helpers import (
            parse_build_sh_repo_data_files,
            parse_build_sh_tools,
        )
        shipped = parse_build_sh_tools() | {
            Path(p).name for p in parse_build_sh_repo_data_files()
        }
        errors = mod.check_required_data_files(shipped)
        assert errors == [], (
            "shipped module missing its required data file: "
            + "; ".join(m for _, m in errors)
        )


# Note: parse_build_sh_tool_paths itself is property-tested in
# tests/shared/test_property_tools.py::TestParseBuildShToolPathsProperties
# (sibling of the parse_build_sh_tools coverage), per the
# property-coverage.yaml manifest convention.


class TestCheckLayoutDepthAssumptions:
    """#1494 — 出貨檔不得靠「數上去幾層」定位 repo 內的東西。

    映像把工具攤平到 ``/opt/da-tools/``（3 個祖先），repo 佈局有 8~9 個，
    所以這一類在 repo 測試裡永遠是綠的、在客戶手上永遠是壞的。
    """

    def test_module_scope_parents_index_is_error(self, tmp_path):
        # 這正是 #1494 的形狀：module scope、import 期就會 IndexError。
        _make_tool(tmp_path, "ops/t.py",
                   "from pathlib import Path\n"
                   "ROOT = Path(__file__).resolve().parents[3] / 'k8s'\n")
        errors = mod.check_layout_depth_assumptions(
            {"ops/t.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        sev, msg = errors[0]
        assert sev == "error"
        assert "ops/t.py:2" in msg and "module scope" in msg

    def test_parent_chain_is_error_even_inside_a_function(self, tmp_path):
        """⛔ 安靜的那一種也要抓。

        ``.parent`` 鏈超過根目錄不會拋錯，它**飽和**在 ``/``，所以只斷言
        「import 不會爆」的守衛看不到它（實測：映像深度下 ``.parent`` 四次
        得到 ``/`` 而非例外）。這一格就是那個差別。
        """
        _make_tool(tmp_path, "dx/t.py",
                   "from pathlib import Path\n"
                   "def f():\n"
                   "    return Path(__file__).resolve()"
                   ".parent.parent.parent.parent\n")
        errors = mod.check_layout_depth_assumptions(
            {"dx/t.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        assert "函式內" in errors[0][1]
        # ⛔ 後果必須由拼法決定：`.parent` 鏈飽和、不拋錯。訊息若說成
        # IndexError，讀到的人會低估（或高估）優先級。
        assert "不會拋錯" in errors[0][1] and "飽和" in errors[0][1]
        assert "IndexError" not in errors[0][1]

    def test_nested_dirname_counts_the_same(self, tmp_path):
        """換 os.path 拼法不是轉綠的路——規則問的是算術不是拼法。"""
        _make_tool(tmp_path, "ops/t.py",
                   "import os\n"
                   "ROOT = os.path.dirname(os.path.dirname(os.path.dirname(\n"
                   "    os.path.abspath(__file__))))\n")
        errors = mod.check_layout_depth_assumptions(
            {"ops/t.py"}, tools_src=tmp_path)
        assert len(errors) == 1

    def test_non_literal_index_is_reported_not_waved_through(self, tmp_path):
        """讀不出來的 index 是「無法擔保」，不是「沒問題」（fail-closed）。"""
        _make_tool(tmp_path, "ops/t.py",
                   "from pathlib import Path\n"
                   "N = 3\n"
                   "ROOT = Path(__file__).resolve().parents[N]\n")
        errors = mod.check_layout_depth_assumptions(
            {"ops/t.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        assert "不是字面常數" in errors[0][1]

    @pytest.mark.parametrize("body", [
        # 恰好踩在映像的合法上限
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n",
        # 同目錄尋址：映像 flat layout 的正解，必須不被擋
        "from pathlib import Path\nD = Path(__file__).resolve().parent / 'x.yaml'\n",
        # 與 __file__ 無關的深度運算不歸這條規則管
        "from pathlib import Path\nROOT = Path('/etc/a/b').parents[2]\n",
    ])
    def test_legal_shapes_stay_green(self, tmp_path, body):
        """⛔ 誤紅面。這三種都是正當寫法，擋掉它們會讓人把規則刪掉。"""
        _make_tool(tmp_path, "ops/t.py", body)
        assert mod.check_layout_depth_assumptions(
            {"ops/t.py"}, tools_src=tmp_path) == []

    def test_unparseable_shipped_file_is_an_error_not_a_skip(self, tmp_path):
        """讀不到就等於沒查過——沉默地略過是 fail-open。"""
        _make_tool(tmp_path, "ops/t.py", "def (\n")
        errors = mod.check_layout_depth_assumptions(
            {"ops/t.py"}, tools_src=tmp_path)
        assert len(errors) == 1
        assert "無法確認" in errors[0][1]

    def test_non_python_entries_are_ignored(self, tmp_path):
        """TOOL_FILES 也含資料檔，別拿 ast 去解析 YAML。"""
        _make_tool(tmp_path, "ops/x.yaml", "a: 1\n")
        assert mod.check_layout_depth_assumptions(
            {"ops/x.yaml"}, tools_src=tmp_path) == []

    def test_actual_repo_has_no_depth_assumptions(self):
        """Repo 級迴歸：今天出貨的 71 支 .py 一處都不許有。"""
        from _lint_helpers import parse_build_sh_tool_paths
        errors = mod.check_layout_depth_assumptions(
            parse_build_sh_tool_paths())
        assert errors == [], (
            "shipped tool(s) still count directory levels:\n"
            + "\n".join(m for _, m in errors)
        )


class TestBuildShArrayReaderMatchesBash:
    """陣列 reader 對「bash 讀得到的東西」不得比 bash 嚴格。"""

    @pytest.mark.parametrize("entry_line,expected", [
        ("    ops/a.py", "ops/a.py"),
        ('    "ops/a.py"', "ops/a.py"),
        # ⛔ 行內註解：bash 視為同一個 word，reader 先前把註解一起吃進檔名，
        # 於是該檔被讀成不存在 → 雙向檢查報「TOOL_FILES 缺少此檔案」，而
        # 訊息完全沒提到註解。最便宜的轉綠是刪掉註解，也就是這條守衛的
        # 實質規則變成「不准替出貨清單寫註解」。
        ("    ops/a.py   # BYO preflight", "ops/a.py"),
        ("    ops/a.py#no-space", "ops/a.py"),
    ])
    def test_entry_forms_bash_accepts(self, tmp_path, entry_line, expected):
        from _lint_helpers import _parse_build_sh_array
        bs = tmp_path / "build.sh"
        bs.write_text(
            "TOOL_FILES=(\n"
            "    # whole-line comment\n"
            f"{entry_line}\n"
            ")\n", encoding="utf-8")
        assert _parse_build_sh_array(bs, "TOOL_FILES") == {expected}

    def test_absent_array_is_empty_not_an_error(self, tmp_path):
        """舊版 build.sh 沒有 REPO_DATA_FILES —— 空集合，不是例外。"""
        from _lint_helpers import _parse_build_sh_array
        bs = tmp_path / "build.sh"
        bs.write_text("TOOL_FILES=(\n    ops/a.py\n)\n", encoding="utf-8")
        assert _parse_build_sh_array(bs, "REPO_DATA_FILES") == set()


class TestRepoDataFilesArePairedWithTheirConsumer:
    """``REPO_DATA_FILES``（build.sh 第二條搬運路徑）也要進配對集合。"""

    def test_repo_data_file_satisfies_required_data_files(self):
        """⛔ 只餵 TOOL_FILES 會把一個真的有出貨的資料檔報成缺漏。

        這一格釘的是 main() 的接線：``configmap-rules-platform.yaml`` 走
        ``REPO_DATA_FILES``（它不在 scripts/tools/ 底下），若配對檢查只看
        ``TOOL_FILES`` 就會對 ``_grar_validate.py`` 誤報。
        """
        from _lint_helpers import (
            parse_build_sh_repo_data_files,
            parse_build_sh_tools,
        )
        repo_data = parse_build_sh_repo_data_files()
        assert repo_data, "REPO_DATA_FILES 解析為空 —— 這一格什麼都沒證明"
        shipped = parse_build_sh_tools() | {
            Path(p).name for p in repo_data
        }
        assert mod.check_required_data_files(shipped) == []

    def test_omitting_the_repo_data_file_is_caught(self):
        """反向控制：拿掉它就必須紅，否則上面那格是恆真的。"""
        from _lint_helpers import parse_build_sh_tools
        errors = mod.check_required_data_files(parse_build_sh_tools())
        assert any("configmap-rules-platform.yaml" in m for _, m in errors), (
            "配對規則沒有在 REPO_DATA_FILES 缺席時開火 —— "
            f"實際得到 {errors}"
        )
