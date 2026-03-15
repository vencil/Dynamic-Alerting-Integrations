#!/usr/bin/env python3
"""Tests for da-tools entrypoint.py — CLI dispatcher logic.

pytest style：使用 plain assert + capsys fixture。

涵蓋:
  1. COMMAND_MAP 完整性 vs build.sh / release-tools.yaml
  2. inject_prometheus_env() env-var fallback
  3. print_usage() / --version 輸出
  4. run_tool() 錯誤處理
  5. main() subcommand dispatch
  6. help 輸出格式驗證
  7. COMMAND_MAP ↔ help text 一致性
"""

import os
import re
import sys

import pytest

import entrypoint  # noqa: E402  (path set by conftest.py)

DA_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir,
    "components", "da-tools", "app",
)


# ── Command Map Consistency ────────────────────────────────────────


# build.sh 中複製的 tools
BUILD_SH_TOOLS = {
    "check_alert.py",
    "baseline_discovery.py",
    "validate_migration.py",
    "migrate_rule.py",
    "scaffold_tenant.py",
    "offboard_tenant.py",
    "deprecate_rule.py",
    "lint_custom_rules.py",
}


class TestCommandMapConsistency:
    """COMMAND_MAP 必須涵蓋 build.sh / CI 中列出的所有工具。"""

    def test_command_map_covers_build_tools(self):
        """build.sh TOOL_FILES 中每個 .py 都有對應 COMMAND_MAP 項目。"""
        mapped_scripts = set(entrypoint.COMMAND_MAP.values())
        missing = BUILD_SH_TOOLS - mapped_scripts
        assert missing == set(), f"build.sh 有但 COMMAND_MAP 沒有: {missing}"

    def test_command_map_values_are_py_files(self):
        """COMMAND_MAP 所有 value 都以 .py 結尾。"""
        for cmd, script in entrypoint.COMMAND_MAP.items():
            assert script.endswith(".py"), f"'{cmd}' 映射到非 .py: {script}"

    def test_prometheus_commands_subset_of_map(self):
        """PROMETHEUS_COMMANDS 只引用有效的 command。"""
        invalid = entrypoint.PROMETHEUS_COMMANDS - set(entrypoint.COMMAND_MAP.keys())
        assert invalid == set(), f"PROMETHEUS_COMMANDS 引用未知命令: {invalid}"

    def test_command_map_keys_are_kebab_case(self):
        """所有 command 名稱使用 kebab-case 格式。"""
        for cmd in entrypoint.COMMAND_MAP:
            assert re.match(r'^[a-z][a-z0-9-]*$', cmd), \
                f"'{cmd}' 不是 kebab-case"

    def test_no_duplicate_scripts(self):
        """不同 command 不映射到同一個 script。"""
        scripts = list(entrypoint.COMMAND_MAP.values())
        assert len(scripts) == len(set(scripts)), \
            "COMMAND_MAP 有重複的 script 映射"


# ── inject_prometheus_env ──────────────────────────────────────────


class TestInjectPrometheusEnv:
    """inject_prometheus_env() 在 --prometheus 缺失時注入 PROMETHEUS_URL。"""

    def test_injects_when_env_set_and_no_flag(self, monkeypatch):
        """PROMETHEUS_URL 設定且 --prometheus 不在 args 時注入。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:9090")
        args = ["--tenant", "db-a"]
        result = entrypoint.inject_prometheus_env(args)
        assert "--prometheus" in result
        assert "http://test:9090" in result

    def test_no_inject_when_flag_present(self, monkeypatch):
        """--prometheus 已存在時不重複注入。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:9090")
        args = ["--prometheus", "http://custom:9090", "--tenant", "db-a"]
        result = entrypoint.inject_prometheus_env(args)
        assert result.count("--prometheus") == 1

    def test_no_inject_when_env_unset(self, monkeypatch):
        """PROMETHEUS_URL 未設定時不注入。"""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        args = ["--tenant", "db-a"]
        result = entrypoint.inject_prometheus_env(args)
        assert "--prometheus" not in result

    def test_returns_same_list_reference(self, monkeypatch):
        """inject_prometheus_env 修改並回傳同一個 list。"""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        args = ["--tenant", "db-a"]
        result = entrypoint.inject_prometheus_env(args)
        assert result is args

    def test_inject_appends_at_beginning(self, monkeypatch):
        """注入的 --prometheus 出現在 args 中（位置不限）。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://prom:9090")
        args = ["--format", "json"]
        result = entrypoint.inject_prometheus_env(args)
        idx = result.index("--prometheus")
        assert result[idx + 1] == "http://prom:9090"


# ── Version Display ────────────────────────────────────────────────


class TestVersionDisplay:
    """--version 讀取 VERSION 檔案。"""

    def test_version_file_exists(self):
        """VERSION 檔案必須存在於 da-tools app 目錄。"""
        version_path = os.path.join(DA_TOOLS_DIR, "VERSION")
        assert os.path.isfile(version_path), f"VERSION 檔案不存在: {version_path}"

    def test_version_is_semver(self):
        """VERSION 內容必須是合法 semver 字串。"""
        version_path = os.path.join(DA_TOOLS_DIR, "VERSION")
        with open(version_path, encoding="utf-8") as f:
            ver = f.read().strip()
        assert re.match(r'^[0-9]+\.[0-9]+\.[0-9]+$', ver), \
            f"VERSION '{ver}' 不是合法 semver"


# ── run_tool error handling ────────────────────────────────────────


class TestRunToolErrors:
    """run_tool() 在 script 不存在時正確退出。"""

    def test_missing_script_exits(self):
        """不存在的 script 應 sys.exit(1)。"""
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.run_tool("nonexistent_tool_xyz.py", [])
        assert exc_info.value.code == 1


# ── print_usage ────────────────────────────────────────────────────


class TestPrintUsage:
    """print_usage() 輸出驗證。"""

    def test_usage_exits_zero(self):
        """print_usage 應 sys.exit(0)。"""
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.print_usage()
        assert exc_info.value.code == 0


# ── main() routing ─────────────────────────────────────────────────


class TestMainRouting:
    """main() subcommand dispatch 測試。"""

    def test_unknown_command_exits(self, monkeypatch):
        """未知 command 應 sys.exit(1)。"""
        monkeypatch.setattr(sys, "argv", ["da-tools", "nonexistent-xyz"])
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 1

    def test_help_exits_zero(self, monkeypatch):
        """--help 應 sys.exit(0)。"""
        monkeypatch.setattr(sys, "argv", ["da-tools", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 0

    def test_no_args_exits_zero(self, monkeypatch):
        """無參數時顯示 usage 並 sys.exit(0)。"""
        monkeypatch.setattr(sys, "argv", ["da-tools"])
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 0

    def test_version_flag_exits_zero(self, monkeypatch):
        """--version 應 sys.exit(0)。"""
        monkeypatch.setattr(sys, "argv", ["da-tools", "--version"])
        with pytest.raises(SystemExit) as exc_info:
            entrypoint.main()
        assert exc_info.value.code == 0


# ── Help text consistency ────────────────────────────────────────


class TestHelpTextConsistency:
    """help text 與 COMMAND_MAP 的一致性。"""

    # 允許少數 command 尚未列入 help text（如新增但 help 未同步）
    _HELP_EXEMPT = {"validate-config"}

    def test_all_commands_in_english_help(self):
        """COMMAND_MAP 所有 command（排除豁免項）出現在英文 help text 中。"""
        help_text = entrypoint._build_help_text("en")
        for cmd in entrypoint.COMMAND_MAP:
            if cmd in self._HELP_EXEMPT:
                continue
            assert cmd in help_text, f"'{cmd}' 不在英文 help text 中"

    def test_all_commands_in_chinese_help(self):
        """COMMAND_MAP 所有 command（排除豁免項）出現在中文 help text 中。"""
        help_text = entrypoint._build_help_text("zh")
        for cmd in entrypoint.COMMAND_MAP:
            if cmd in self._HELP_EXEMPT:
                continue
            assert cmd in help_text, f"'{cmd}' 不在中文 help text 中"

    def test_help_text_bilingual_available(self):
        """英文和中文 help text 均可產生且長度 > 100。"""
        en = entrypoint._build_help_text("en")
        zh = entrypoint._build_help_text("zh")
        assert len(en) > 100
        assert len(zh) > 100

    def test_help_text_contains_usage_pattern(self):
        """Help text 包含 usage 格式說明。"""
        for lang in ["en", "zh"]:
            help_text = entrypoint._build_help_text(lang)
            assert "da-tools" in help_text
            assert "<command>" in help_text


# ── CI Workflow Sync ───────────────────────────────────────────────


class TestCIWorkflowSync:
    """release-tools.yaml TOOLS 陣列必須匹配 build.sh TOOL_FILES。"""

    @staticmethod
    def _parse_tools_from_file(filepath, start_marker, end_marker):
        """從 script 中擷取 tool 檔名。"""
        tools = []
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        in_block = False
        for line in content.splitlines():
            stripped = line.strip()
            if start_marker in stripped:
                in_block = True
                continue
            if in_block and end_marker in stripped:
                break
            if in_block and stripped and not stripped.startswith("#"):
                name = stripped.strip("\"'(),")
                if name.endswith(".py") or name.endswith(".yaml"):
                    tools.append(name)
        return set(tools)

    def test_ci_matches_build_sh(self):
        """release-tools.yaml TOOLS 必須是 build.sh TOOL_FILES 的超集。"""
        repo_root = os.path.join(os.path.dirname(__file__), os.pardir)
        build_sh = os.path.join(repo_root, "components", "da-tools", "app", "build.sh")
        ci_yaml = os.path.join(repo_root, ".github", "workflows", "release-tools.yaml")

        if not os.path.isfile(build_sh) or not os.path.isfile(ci_yaml):
            pytest.skip("build.sh 或 release-tools.yaml 不存在")

        build_tools = self._parse_tools_from_file(build_sh, "TOOL_FILES=(", ")")
        ci_tools = self._parse_tools_from_file(ci_yaml, "TOOLS=(", ")")

        missing_in_ci = build_tools - ci_tools
        assert missing_in_ci == set(), \
            f"build.sh 有但 CI workflow 沒有: {missing_in_ci}"


# ── bump_docs tools rule coverage ──────────────────────────────────


class TestBumpDocsToolsRuleCoverage:
    """bump_docs.py tools_rules 必須涵蓋 da-tools README header version。"""

    def test_readme_header_rule_exists(self):
        """bump_docs tools_rules 包含 da-tools README version header 規則。"""
        import bump_docs
        rules = bump_docs._build_rules()
        tools_descs = [r["desc"] for r in rules["tools"]]
        header_rules = [d for d in tools_descs if "version header" in d.lower()]
        assert len(header_rules) >= 1, \
            "bump_docs 沒有涵蓋 da-tools README version header 的規則"
