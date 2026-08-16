#!/usr/bin/env python3
"""test_bump_docs.py — 版號一致性管理工具 測試套件 (Wave 12 pytest 遷移)。

驗證 bump_docs.py 的核心功能:
  1. _build_rules() 規則結構完整性
  2. apply_rules() check-only 與寫入模式
  3. read_current_versions() 版號讀取
  4. Lambda replacement 正確性

用法:
  python3 -m pytest tests/test_bump_docs.py -v
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Add scripts/tools to path

import bump_docs  # noqa: E402


class TestBuildRules:
    """測試 _build_rules() 規則結構。"""

    def test_returns_all_version_lines(self):
        rules = bump_docs._build_rules()
        assert "platform" in rules
        assert "exporter" in rules
        assert "tools" in rules
        assert "portal" in rules
        assert "tenant-api" in rules

    def test_all_rules_have_required_keys(self):
        rules = bump_docs._build_rules()
        for line_name, line_rules in rules.items():
            for rule in line_rules:
                assert "file" in rule, f"Missing 'file' in {line_name} rule"
                assert "desc" in rule, f"Missing 'desc' in {line_name} rule"
                assert "replacement" in rule, f"Missing 'replacement' in {line_name} rule"
                # Either 'pattern' or 'whole_file' must exist
                has_pattern = "pattern" in rule or "whole_file" in rule
                assert has_pattern, f"Missing pattern/whole_file in {line_name} rule"

    def test_tools_rules_reference_da_tools(self):
        """da-tools 規則應引用 da-tools 相關檔案。"""
        rules = bump_docs._build_rules()
        tool_files = [r["file"] for r in rules["tools"]]
        assert any("da-tools" in f for f in tool_files)

    def test_portal_rules_reference_da_portal(self):
        """da-portal 規則應引用 da-portal 相關檔案（5th release line）。"""
        rules = bump_docs._build_rules()
        portal_files = [r["file"] for r in rules["portal"]]
        assert any("da-portal" in f for f in portal_files)

    def test_platform_rules_reference_chart(self):
        """Chart.yaml 版號規則應存在於 exporter rules（chart 版號 = exporter 版號）。"""
        rules = bump_docs._build_rules()
        exporter_files = [r["file"] for r in rules["exporter"]]
        assert any("Chart.yaml" in f for f in exporter_files)


class TestApplyRulesCheckOnly:
    """測試 apply_rules() check-only 模式。"""

    def test_check_detects_outdated(self, monkeypatch):
        """Outdated 版號應被偵測為 UPDATE。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake file with old version
            test_file = os.path.join(tmpdir, "test.md")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("Image: ghcr.io/vencil/da-tools:0.1.0\n")
            os.chmod(test_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            rules = [{
                "file": "test.md",
                "desc": "test image tag",
                "pattern": r"ghcr\.io/vencil/da-tools:[0-9]+\.[0-9]+\.[0-9]+",
                "replacement": lambda v: f"ghcr.io/vencil/da-tools:{v}",
            }]

            # Override REPO_ROOT temporarily using monkeypatch
            monkeypatch.setattr(bump_docs, "REPO_ROOT", Path(tmpdir))
            changes = bump_docs.apply_rules(rules, "0.2.0", check_only=True)
            statuses = [c[0] for c in changes]
            assert "UPDATE" in statuses

            # File should NOT be modified in check mode
            with open(test_file, 'r') as f:
                content = f.read()
            assert "0.1.0" in content

    def test_check_passes_when_current(self, monkeypatch):
        """已更新的版號應回傳 OK。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.md")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("Image: ghcr.io/vencil/da-tools:0.2.0\n")
            os.chmod(test_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            rules = [{
                "file": "test.md",
                "desc": "test image tag",
                "pattern": r"ghcr\.io/vencil/da-tools:[0-9]+\.[0-9]+\.[0-9]+",
                "replacement": lambda v: f"ghcr.io/vencil/da-tools:{v}",
            }]

            monkeypatch.setattr(bump_docs, "REPO_ROOT", Path(tmpdir))
            changes = bump_docs.apply_rules(rules, "0.2.0", check_only=True)
            statuses = [c[0] for c in changes]
            assert "OK" in statuses


class TestApplyRulesWrite:
    """測試 apply_rules() 寫入模式。"""

    def test_write_updates_file(self, monkeypatch):
        """寫入模式應實際修改檔案。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.md")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("Version: ghcr.io/vencil/da-tools:0.1.0 end\n")
            os.chmod(test_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            rules = [{
                "file": "test.md",
                "desc": "test tag",
                "pattern": r"ghcr\.io/vencil/da-tools:[0-9]+\.[0-9]+\.[0-9]+",
                "replacement": lambda v: f"ghcr.io/vencil/da-tools:{v}",
            }]

            monkeypatch.setattr(bump_docs, "REPO_ROOT", Path(tmpdir))
            changes = bump_docs.apply_rules(rules, "0.3.0", check_only=False)
            statuses = [c[0] for c in changes]
            assert "UPDATE" in statuses

            # File should be modified
            with open(test_file, 'r') as f:
                content = f.read()
            assert "0.3.0" in content
            assert "0.1.0" not in content

    def test_whole_file_mode(self, monkeypatch):
        """whole_file 模式應替換整個檔案內容。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "VERSION")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("0.1.0\n")
            os.chmod(test_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            rules = [{
                "file": "VERSION",
                "desc": "VERSION file",
                "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+\s*$",
                "replacement": lambda v: f"{v}\n",
                "whole_file": True,
            }]

            monkeypatch.setattr(bump_docs, "REPO_ROOT", Path(tmpdir))
            bump_docs.apply_rules(rules, "0.2.0", check_only=False)

            with open(test_file, 'r') as f:
                content = f.read()
            assert content.strip() == "0.2.0"


class TestApplyRulesEdgeCases:
    """邊界案例。"""

    def test_missing_file_returns_skip(self, monkeypatch):
        """檔案不存在應回傳 SKIP。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rules = [{"file": "nonexistent.md", "desc": "missing",
                       "pattern": r"v\d+", "replacement": lambda v: f"v{v}"}]
            monkeypatch.setattr(bump_docs, "REPO_ROOT", Path(tmpdir))
            changes = bump_docs.apply_rules(rules, "1.0.0", check_only=True)
            assert changes[0][0] == "SKIP"

    def test_no_match_returns_ok_only_when_opted_out(self, monkeypatch):
        """Pattern 不匹配時，只有明示 `require_match: False` 才回 OK。

        這條原本斷言「手寫規則撈不到 = OK」，正是 #1407 修掉的行為：手寫規則
        指名單一檔案與單一句型，撈不到就是壞了。保留 OK 這一半的覆蓋，但改成
        測 opt-out 路徑——預設值的兩個方向由 TestRequireMatchDefaults 顧。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.md")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("No version here\n")
            os.chmod(test_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            rules = [{"file": "test.md", "desc": "test",
                       "pattern": r"v\d+\.\d+\.\d+",
                       "replacement": lambda v: f"v{v}",
                       "require_match": False}]
            monkeypatch.setattr(bump_docs, "REPO_ROOT", Path(tmpdir))
            changes = bump_docs.apply_rules(rules, "1.0.0", check_only=True)
            assert changes[0][0] == "OK"


class TestReadCurrentVersions:
    """測試版號讀取。"""

    @pytest.mark.parametrize("line", bump_docs.VERSION_LINES)
    def test_reads_from_real_repo(self, line):
        """六條線在今天的 repo 裡都必須讀得到，一條都不能少。

        原本這條只斷言 platform/exporter/tools/portal 四條，而且整個包在
        `if <chart>.exists()` 裡——recipe-preview 沒有被任何地方斷言過，
        tenant-api 也是補上 read 之後才有人看。參數化到六條之後，新增第七條
        版號線時 VERSION_LINES 一改，這條就自動跟著要求它可讀。
        """
        versions = bump_docs.read_current_versions()
        src, shape = bump_docs.VERSION_LINE_SOURCES[line]
        assert versions.get(line), (
            f"版號線 {line} 讀不到 SSOT：{src} 裡的 {shape}。"
            f"少了它，這條線的所有規則都不會被評估——而在 #1407 第二輪修掉之前，"
            f"那等於整條線從 --check 的視野裡消失且 exit 0。")


class TestFilterByScope:
    """_filter_by_scope() 範圍過濾。"""

    def test_no_scope_returns_all(self):
        """空 scope 回傳全部 rules。"""
        rules = [{"file": "docs/x.md"}, {"file": "README.md"}]
        result = bump_docs._filter_by_scope(rules, None)
        assert len(result) == 2

    def test_scope_filters_files(self):
        """scope 正確過濾檔案。"""
        rules = [
            {"file": "docs/a.md"},
            {"file": "docs/b.md"},
            {"file": "components/x.py"},
        ]
        result = bump_docs._filter_by_scope(rules, "docs")
        assert len(result) == 2

    def test_root_files_included_with_dot(self):
        """scope='.' 包含根目錄檔案。"""
        rules = [{"file": "README.md"}, {"file": "CLAUDE.md"}]
        result = bump_docs._filter_by_scope(rules, ".")
        assert len(result) == 2

    def test_glob_rule_scope(self):
        """__glob__ rule 根據 glob_dir 過濾。"""
        rules = [{"file": "__glob__", "glob_dir": "docs/scenarios"}]
        result = bump_docs._filter_by_scope(rules, "docs")
        assert len(result) == 1

    # ── 以下三條補的是「上面四條用 startswith 實作也全綠」的破口 ──
    #
    # ⛔ 上面全部只用「完全相等的 segment 名」當 scope，所以
    # `_path_is_under` 換成 `path.startswith(scope)` 之後它們一條都不會紅。
    # 實測損害：`--check --scope doc`（少打一個 s）從
    # `rc=2 ERROR: selected ZERO rules` 變成 `rc=0 ✅ All version references
    # are consistent`——「有選到東西」不等於「選到你要的東西」，而兩個
    # 空值守門員只看得見前者。

    def test_a_prefix_that_is_not_a_segment_selects_nothing(self):
        """`doc` 不是 `docs/a.md` 的父目錄——這是 `_path_is_under` docstring
        自己舉的例子，而它從來沒有被斷言過。
        """
        assert bump_docs._path_is_under("docs/a.md", "docs") is True
        assert bump_docs._path_is_under("docs/a.md", "doc") is False, (
            "`doc` 被當成 `docs/` 的前綴——字串前綴不是路徑包含。")
        rules = [{"file": "docs/a.md"}, {"file": "docs/b.md"}]
        assert bump_docs._filter_by_scope(rules, "doc") == [], (
            "一個字母的 typo 選到了整棵 docs/ 樹，而 --scope 的兩個空值守門員"
            "都會因此放行。")

    def test_a_prefix_that_is_not_a_segment_also_misses_glob_dirs(self):
        """glob 那條腿走的是 glob_dir，同樣不能吃字串前綴。"""
        rules = [{"file": "__glob__", "glob_dir": "docs"}]
        assert bump_docs._filter_by_scope(rules, "doc") == [], (
            "`--scope doc` 選到了 glob_dir `docs` 的規則——這正是那個 typo "
            "能一路走到「✅ 全部一致」的原因（glob 有被選到，所以非空守門員"
            "以為一切正常，而所有手寫的 docs/** 規則被靜靜丟掉）。")

    def test_a_glob_rooted_above_the_scope_is_kept(self):
        """⛔ 雙向包含：`docs/**` 的 glob 對 `--scope docs/integration` 必須留下。

        既有的 `test_glob_rule_scope` 只走了「glob 比 scope 窄」那個方向，
        所以把過濾改成單向（只留 glob_dir 在 scope 底下的）照樣全綠。實測
        損害：`--scope docs/integration` 從 42 個檔掉到 8 個——被丟掉的正是
        那 34 條「治理被 scope 到的那些檔案」的 docs/** front-matter 規則，
        也就是原始碼那段 ⛔ 註解說絕不能掉的東西。
        """
        rules = [{"file": "__glob__", "glob_dir": "docs"}]
        assert len(bump_docs._filter_by_scope(rules, "docs/integration")) == 1, (
            "glob 掛在 scope 上方就被丟掉了——那條 glob 擁有的正是被 scope "
            "的那批檔案。")


class TestInitChangelog:
    """_init_changelog_entry() 測試。"""

    def test_zh_changelog_stub(self, tmp_path, monkeypatch):
        """中文 CHANGELOG stub 插入。"""
        cl = tmp_path / "CHANGELOG.md"
        cl.write_text("# Changelog\n\n## [v1.0.0] — Initial\n",
                      encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        bump_docs._init_changelog_entry("2.0.0", lang="zh")
        content = cl.read_text(encoding="utf-8")
        assert "v2.0.0" in content
        assert "版號" in content

    def test_en_changelog_stub(self, tmp_path, monkeypatch):
        """英文 CHANGELOG.en.md stub 插入。"""
        cl = tmp_path / "CHANGELOG.en.md"
        cl.write_text("# Changelog\n\n## [v1.0.0] — Initial\n",
                      encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        bump_docs._init_changelog_entry("2.0.0", lang="en")
        content = cl.read_text(encoding="utf-8")
        assert "v2.0.0" in content
        assert "Versions" in content

    def test_all_changelog_stubs(self, tmp_path, monkeypatch):
        """lang='all' 同時插入 zh + en。"""
        (tmp_path / "CHANGELOG.md").write_text(
            "# CL\n\n## [v1.0.0]\n", encoding="utf-8")
        (tmp_path / "CHANGELOG.en.md").write_text(
            "# CL\n\n## [v1.0.0]\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        bump_docs._init_changelog_entry("2.0.0", lang="all")
        assert "v2.0.0" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "v2.0.0" in (tmp_path / "CHANGELOG.en.md").read_text(encoding="utf-8")


import sys


class TestMainCLI:
    """main() CLI 路徑覆蓋。"""

    def test_show_current(self, monkeypatch, capsys, cli_argv):
        """--show-current 顯示全部六條線（讀不到的印 NOT FOUND 並 exit 1）。"""
        cli_argv("bump_docs", "--show-current")
        bump_docs.main()          # 真實 repo 六條都讀得到 → 不應 SystemExit
        out = capsys.readouterr().out
        assert "Current versions" in out
        for line in bump_docs.VERSION_LINES:
            assert line in out, f"--show-current 沒列出 {line}"

    def test_check_only(self, monkeypatch, capsys, cli_argv):
        """--check 模式不修改檔案。"""
        cli_argv("bump_docs", "--platform", "99.99.99", "--check")
        # Should not raise (just reports mismatches)
        try:
            bump_docs.main()
        except SystemExit:
            pass  # exit 1 if outdated — expected
        out = capsys.readouterr().out
        assert "platform" in out.lower() or "PLATFORM" in out or len(out) > 0

    def test_dry_run(self, monkeypatch, capsys, cli_argv):
        """--dry-run 顯示差異但不修改。"""
        cli_argv("bump_docs", "--platform", "99.99.99", "--dry-run")
        bump_docs.main()
        out = capsys.readouterr().out
        # Should show diffs or "no changes"
        assert len(out) > 0

    def test_init_changelog(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--init-changelog 插入 stub。"""
        cl = tmp_path / "CHANGELOG.md"
        cl.write_text("# CL\n\n## [v1.0.0]\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        cli_argv("bump_docs", "--init-changelog", "3.0.0")
        bump_docs.main()
        assert "v3.0.0" in cl.read_text(encoding="utf-8")

    def test_what_if(self, monkeypatch, capsys, cli_argv):
        """--what-if 顯示規則審計。"""
        cli_argv("bump_docs", "--what-if")
        try:
            bump_docs.main()
        except SystemExit:
            pass  # might exit 1 if versions not found
        out = capsys.readouterr().out
        # Should show some rule audit output
        assert len(out) > 0

    def test_scope_flag(self, monkeypatch, capsys, cli_argv):
        """--scope 限制範圍。"""
        cli_argv("bump_docs", "--platform", "99.99.99",
            "--check", "--scope", "docs")
        try:
            bump_docs.main()
        except SystemExit:
            pass
        out = capsys.readouterr().out
        assert len(out) > 0


class TestPhaseAddedCountRules:
    """v2.8.0 Phase A review — count drift detection added in this PR.

    Two new rules were wired into _build_count_rules() to catch hardcoded
    counts that previously drifted silently:

      1. CLAUDE.md "N auto-run + M manual-stage + K pre-push hooks"
         — the pre-existing rule expected the old "N 個 auto-run hooks
         （每次 commit）" format which CLAUDE.md no longer uses (S#87
         slim-down rewrote the line). Drift went undetected.

      2. dev-rules.md "專案有 **N 個 JSX 互動工具**" — bumped from 39 to
         43 during Phase .c (master-onboarding / alert-builder /
         routing-trace / simulate-preview), but no rule existed.

    A separate fix updates _count_jsx_tools() to match top-level `- key:`
    in tool-registry.yaml (the registry uses `tools:\n- key:` shape, not
    `tools:\n  - key:` — the previous regex required 2-space indent and
    silently returned 0).
    """

    def test_count_jsx_tools_handles_top_level_dash(self, tmp_path,
                                                     monkeypatch):
        """_count_jsx_tools accepts both nested and top-level `- key:`."""
        registry = tmp_path / "docs" / "assets" / "tool-registry.yaml"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            "tools:\n"
            "- key: alpha\n"
            "  title: Alpha\n"
            "- key: beta\n"
            "  title: Beta\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        assert bump_docs._count_jsx_tools() == 2

    def test_count_jsx_tools_handles_legacy_nested(self, tmp_path,
                                                     monkeypatch):
        """Legacy 2-space-indented tool-registry shape still counted."""
        registry = tmp_path / "docs" / "assets" / "tool-registry.yaml"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            "tools:\n"
            "  - key: alpha\n"
            "  - key: beta\n"
            "  - key: gamma\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        assert bump_docs._count_jsx_tools() == 3

    def test_count_precommit_hook_stages_classifies_correctly(
            self, tmp_path, monkeypatch):
        """Hook stage classifier returns (auto, manual, pre-push)."""
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text(
            "default_stages: [pre-commit]\n"
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: a-default\n"
            "      - id: b-explicit-pre-commit\n"
            "        stages: [pre-commit]\n"
            "      - id: c-manual\n"
            "        stages: [manual]\n"
            "      - id: d-pre-push\n"
            "        stages: [pre-push]\n"
            "      - id: e-manual\n"
            "        stages: [manual]\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        auto, manual, push = bump_docs._count_precommit_hook_stages()
        assert (auto, manual, push) == (2, 2, 1)

    def test_count_hook_stages_missing_config_returns_zeros(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        assert bump_docs._count_precommit_hook_stages() == (0, 0, 0)

    def test_hook_breakdown_rule_matches_new_text_shape(
            self, tmp_path, monkeypatch):
        """The hook-breakdown rule pattern matches the post-S#87 text."""
        # Stage a tmp repo with a tool-registry + .pre-commit-config so
        # rule registration sees non-zero counts (rules are conditionally
        # registered when their source count is positive).
        (tmp_path / "docs" / "assets").mkdir(parents=True)
        (tmp_path / "docs" / "assets" / "tool-registry.yaml").write_text(
            "tools:\n- key: x\n", encoding="utf-8")
        (tmp_path / ".pre-commit-config.yaml").write_text(
            "repos:\n  - repo: local\n    hooks:\n      - id: a\n",
            encoding="utf-8")

        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        rules = bump_docs._build_count_rules()
        hook_rules = [r for r in rules
                      if "pre-commit hook breakdown" in r["desc"]]
        assert len(hook_rules) == 1
        rule = hook_rules[0]

        # Old format (pre-S#87): `13 個 auto-run hooks（每次 commit）`
        # New format (post-S#87): `39 auto-run + 14 manual-stage + 3 pre-push hooks`
        import re as _re
        new_text = "39 auto-run + 14 manual-stage + 3 pre-push hooks"
        assert _re.search(rule["pattern"], new_text), (
            "Hook-breakdown pattern must match the post-S#87 CLAUDE.md text."
        )
        # Backward compatibility: pattern also accepts the no-pre-push
        # 2-stage shape `N auto-run + M manual-stage hooks`.
        legacy_2stage = "12 auto-run + 5 manual-stage hooks"
        assert _re.search(rule["pattern"], legacy_2stage)

    def test_jsx_tools_dev_rules_rule_matches_dev_rules_text(
            self, tmp_path, monkeypatch):
        """The new dev-rules.md JSX count rule pattern matches the SOP heading."""
        (tmp_path / "docs" / "assets").mkdir(parents=True)
        (tmp_path / "docs" / "assets" / "tool-registry.yaml").write_text(
            "tools:\n- key: x\n- key: y\n", encoding="utf-8")
        (tmp_path / ".pre-commit-config.yaml").write_text(
            "repos:\n", encoding="utf-8")

        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        rules = bump_docs._build_count_rules()
        jsx_rules = [r for r in rules
                     if r["file"] == "docs/internal/dev-rules.md"]
        assert len(jsx_rules) == 1
        rule = jsx_rules[0]

        import re as _re
        sample = "專案有 **39 個 JSX 互動工具**，Source of Truth 檔案："
        m = _re.search(rule["pattern"], sample)
        assert m, "JSX-count pattern must match the dev-rules.md SOP heading."


class TestSkipReleasedChangelog:
    """PR #503 regression — the inline-version-text rule must not bump
    historical version refs frozen inside released `## [vX.Y.Z]` CHANGELOG
    entries.

    The rule scans docs/**/*.md and docs/CHANGELOG.md symlinks to the root
    CHANGELOG. Before the fix it false-matched a `已於 v<old>` sentence in
    the latest released entry and wanted to flip that past-release fact to
    the version being bumped to.

    Fixtures use synthetic versions (0.x.0) — the same convention as the
    rest of this file — so they never read as the real platform version.
    """

    def _inline_rule(self):
        """The real `inline version text in doc content` platform rule."""
        platform_rules = bump_docs._build_rules()["platform"]
        inline = [r for r in platform_rules
                  if r.get("desc") == "inline version text in doc content"]
        assert len(inline) == 1, "expected exactly one inline-version rule"
        return inline[0]

    def test_inline_rule_opts_into_skip(self):
        """The shipped rule carries skip_released_changelog."""
        assert self._inline_rule().get("skip_released_changelog") is True

    def test_split_helper_splits_at_first_released_heading(self):
        content = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n- 進行中\n\n"
            "## [v0.2.0] — synthetic (2026-01-02)\n\n- 已於 v0.1.0 刪除\n\n"
            "## [v0.1.0]\n\n- 更早的條目\n"
        )
        live, frozen = bump_docs._split_at_released_changelog(content)
        assert live + frozen == content
        assert "## [Unreleased]" in live
        assert "## [v0.2.0]" in frozen
        assert "## [v0.1.0]" in frozen
        assert "已於 v0.1.0" in frozen

    def test_split_helper_noop_without_released_heading(self):
        """Ordinary docs (no `## [vX.Y.Z]`) come back fully-live."""
        content = "# 指南\n\n本指南對應於 v0.1.0 平台。\n"
        live, frozen = bump_docs._split_at_released_changelog(content)
        assert live == content
        assert frozen == ""

    def test_historical_ref_in_released_section_not_bumped(
            self, tmp_path, monkeypatch):
        """#503 case: a `已於 v<old>` fact inside the latest released entry
        stays put when the platform version is bumped."""
        docs = tmp_path / "docs"
        docs.mkdir()
        changelog = docs / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "- 待發佈內容。\n\n"
            "## [v0.2.0] — synthetic release (2026-01-02)\n\n"
            "- `some-doc.md` 已於 v0.1.0 被 phantom-delete。\n",
            encoding="utf-8",
        )
        os.chmod(changelog,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

        changes = bump_docs.apply_rules(
            [self._inline_rule()], "0.2.0", check_only=False)

        result = changelog.read_text(encoding="utf-8")
        # Historical fact left intact — not flipped to the bump target.
        assert "已於 v0.1.0 被 phantom-delete" in result
        assert "v0.2.0 被 phantom-delete" not in result
        assert "UPDATE" not in [c[0] for c in changes]

    def test_inline_ref_in_ordinary_doc_still_bumped(
            self, tmp_path, monkeypatch):
        """A doc with no released-version heading is unaffected by the skip
        — the inline rule still bumps `於 vX.Y.Z` there (no over-skip)."""
        docs = tmp_path / "docs"
        docs.mkdir()
        guide = docs / "guide.md"
        guide.write_text(
            "# 平台指南\n\n本指南對應於 v0.1.0 平台。\n",
            encoding="utf-8",
        )
        os.chmod(guide,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

        changes = bump_docs.apply_rules(
            [self._inline_rule()], "0.2.0", check_only=False)

        assert "對應於 v0.2.0 平台" in guide.read_text(encoding="utf-8")
        assert "UPDATE" in [c[0] for c in changes]

    def test_check_clean_when_only_frozen_drift(self, tmp_path, monkeypatch):
        """--check reports no drift when the only stale ref is inside a
        frozen released entry, and leaves the file untouched."""
        docs = tmp_path / "docs"
        docs.mkdir()
        changelog = docs / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n- 待發佈。\n\n"
            "## [v0.1.0] — synthetic initial (2026-01-01)\n\n"
            "- 行為於 v0.0.9 調整。\n",
            encoding="utf-8",
        )
        os.chmod(changelog,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

        changes = bump_docs.apply_rules(
            [self._inline_rule()], "0.2.0", check_only=True)
        assert "UPDATE" not in [c[0] for c in changes]
        assert "行為於 v0.0.9 調整" in changelog.read_text(encoding="utf-8")


class TestHelmTwoLinePin:
    """helm/federation-reconciler's `repository:` + `tag:` pin (F3).

    That pin went unbumped for two releases because every rule here is
    line-oriented and a Helm values pin spans two lines. It is not merely a
    stale doc: the image-pin capability gate
    (scripts/tools/lint/check_image_pin_capability.py) exempts that chart
    *because* the pinned tag lacks the reconciler script, and the exemption's
    exit condition is "the pin gets bumped". Nothing bumping the pin ⇒ the
    exemption can never go stale ⇒ a severity:critical ADR-028 control stays
    broken with every gate green. These tests pin the driver.
    """

    _VALUES = (
        "image:\n"
        "  repository: ghcr.io/vencil/da-tools\n"
        '  tag: "v2.9.0"\n'
        '  digest: ""\n'
        "  pullPolicy: IfNotPresent\n"
        "\n"
        "sidecar:\n"
        "  repository: quay.io/other/thing\n"
        '  tag: "v1.0.0"\n'
    )

    def _tools_rules_for(self, path):
        return [r for r in bump_docs._build_rules()["tools"] if r["file"] == path]

    def test_rules_exist_for_both_files(self):
        assert self._tools_rules_for("helm/federation-reconciler/values.yaml")
        assert self._tools_rules_for("helm/federation-reconciler/Chart.yaml")

    def test_real_repo_rules_bump_both_files(self, tmp_path, monkeypatch):
        """End-to-end on the REAL rules against a copy of the real files."""
        chart_dir = tmp_path / "helm" / "federation-reconciler"
        chart_dir.mkdir(parents=True)
        real = bump_docs.REPO_ROOT / "helm" / "federation-reconciler"
        for name in ("values.yaml", "Chart.yaml"):
            (chart_dir / name).write_text(
                (real / name).read_text(encoding="utf-8"), encoding="utf-8")

        rules = (self._tools_rules_for("helm/federation-reconciler/values.yaml")
                 + self._tools_rules_for("helm/federation-reconciler/Chart.yaml"))
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules(rules, "2.10.0", check_only=False)

        assert [c[0] for c in changes] == ["UPDATE", "UPDATE"], changes
        values_text = (chart_dir / "values.yaml").read_text(encoding="utf-8")
        assert '  tag: "v2.10.0"\n' in values_text
        assert '"v2.9.0"' not in values_text.split("image:")[1].split("store:")[0]
        assert 'appVersion: "v2.10.0"' in (chart_dir / "Chart.yaml").read_text(
            encoding="utf-8")

    def test_pair_rule_ignores_a_sibling_image(self, tmp_path, monkeypatch):
        """Matching `tag:` alone would rewrite the wrong image's tag."""
        values = tmp_path / "values.yaml"
        values.write_text(self._VALUES, encoding="utf-8")
        rule = dict(self._tools_rules_for(
            "helm/federation-reconciler/values.yaml")[0], file="values.yaml")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        bump_docs.apply_rules([rule], "2.10.0", check_only=False)
        text = values.read_text(encoding="utf-8")
        assert '  tag: "v2.10.0"\n' in text
        assert '  tag: "v1.0.0"\n' in text  # the sidecar is untouched

    def test_pair_rule_preserves_indentation(self, tmp_path, monkeypatch):
        """A 4-space chart must not be silently re-indented (= broken YAML)."""
        values = tmp_path / "values.yaml"
        values.write_text(
            "image:\n"
            "    repository: ghcr.io/vencil/da-tools\n"
            '    tag: "v2.9.0"\n',
            encoding="utf-8")
        rule = dict(self._tools_rules_for(
            "helm/federation-reconciler/values.yaml")[0], file="values.yaml")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        bump_docs.apply_rules([rule], "2.10.0", check_only=False)
        assert '    tag: "v2.10.0"\n' in values.read_text(encoding="utf-8")

    def test_dead_pair_rule_is_reported_not_silently_ok(self, tmp_path, monkeypatch):
        """If the shape moves, the rule must DIE LOUDLY.

        "no match → OK" is exactly how this pin escaped notice for two
        releases; a rule that drives an exit condition may not fail silently.
        """
        values = tmp_path / "values.yaml"
        values.write_text("image:\n  repo: ghcr.io/vencil/da-tools\n", encoding="utf-8")
        rule = dict(self._tools_rules_for(
            "helm/federation-reconciler/values.yaml")[0], file="values.yaml")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([rule], "2.10.0", check_only=True)
        assert changes[0][0] == "DEAD", changes
        assert "matched NOTHING" in changes[0][2]

    def test_dead_require_match_line_rule_is_reported(self, tmp_path, monkeypatch):
        """The same guarantee for the line-oriented Chart.yaml appVersion rule."""
        chart = tmp_path / "Chart.yaml"
        chart.write_text("apiVersion: v2\nname: probe\nversion: 0.1.0\n", encoding="utf-8")
        rule = dict(self._tools_rules_for(
            "helm/federation-reconciler/Chart.yaml")[0], file="Chart.yaml")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([rule], "2.10.0", check_only=True)
        assert changes[0][0] == "DEAD", changes

    def test_live_repo_rules_are_not_dead(self):
        """The rules must actually match the files as they are committed today."""
        rules = (self._tools_rules_for("helm/federation-reconciler/values.yaml")
                 + self._tools_rules_for("helm/federation-reconciler/Chart.yaml"))
        version = bump_docs.read_current_versions()["tools"]
        changes = bump_docs.apply_rules(rules, version, check_only=True)
        assert [c[0] for c in changes] == ["OK", "OK"], changes


class TestLiveRepoRuleTargetsExist:
    """每一條規則指向的檔案，在今天的 repo 裡都必須真的存在。

    上面那條 live-repo 測試只顧 federation-reconciler 兩條規則，其餘上千條
    無人看守。規則指向不存在的檔案時 apply_rules() 只回 SKIP，而在 #1407
    修掉之前所有 consumer 都不看 SKIP——於是 14 條規則（11 個路徑）在
    `make version-check` 全綠的情況下靜靜死了好幾個版本：docs/ 底下的整合
    指南搬進 docs/integration/、interactive JSX 搬去 tools/portal/src/、
    GitLab CI 範本整個被刪，沒有任何一個 gate 出聲。

    這條測試是那個漏洞的機械守門員：走完 _build_rules() 全集，任何指向
    不存在檔案的規則直接讓測試紅，且訊息裡直接點名路徑。
    """

    # _build_rules() 目前產出約 1900 條（含 glob 展開）。這個下限純粹是
    # anti-vacuity：把 _build_rules() 改窄成「什麼都不描述」時，這條測試
    # 不能因為集合變空就自動變綠。故意抓得比實際低很多，正常增刪規則不會誤觸。
    _MIN_RULES = 500

    def test_every_rule_target_file_exists(self):
        rules = bump_docs._expand_glob_rules(
            [r for line_rules in bump_docs._build_rules().values()
             for r in line_rules])

        assert len(rules) >= self._MIN_RULES, (
            f"_build_rules() 只產出 {len(rules)} 條規則（下限 "
            f"{self._MIN_RULES}）。規則集被改窄到這個程度時，"
            f"「沒有死規則」是因為幾乎沒有規則，不是因為健康。")

        missing = sorted({
            r["file"] for r in rules
            if not r.get("glob_collapsed")          # 由 GLOB-EMPTY 負責診斷
            and not (bump_docs.REPO_ROOT / r["file"]).exists()})

        assert not missing, (
            f"{len(missing)} 個規則目標檔案不存在——這些規則什麼都 bump 不到，"
            f"而且在 --check 裡只會是一行 MISSING：\n  "
            + "\n  ".join(missing)
            + "\n修法：改 _build_*_rules() 裡該規則的 \"file\" 指向現行路徑；"
              "若目標已徹底消失（例如範本改由產生器產出），整條規則刪掉。")


class TestGlobRulesExpandToFiles:
    """每一條 `__glob__` 規則都必須至少展開到 1 個檔案。

    這條測試補的是上面 TestLiveRepoRuleTargetsExist 看不到的死角。那條走的是
    「展開後的規則指向的檔案存在嗎」——但 glob 展開到 **零** 個檔案時，根本不會
    產生任何規則條目：沒有 SKIP、沒有 DEAD、沒有 MISSING，連 `_MIN_RULES`
    下限都擋不住（一條 glob 從數百塌成 0，總數仍遠高於下限）。整條規則就這樣
    從所有 gate 的視野裡消失。

    實測後果（#1407）：JSX 在 TRK-230 搬去 tools/portal/src/ 之後，兩條
    `glob_dir: docs` + `glob_pattern: **/*.jsx` 的規則展開成 0，於是 44 個
    JSX front matter 停在 v2.7.0，而平台 SSOT 是 2.9.0、`--check` 全綠。

    斷言的是 glob 的**存在意義**：一條展開到 0 個檔案的 glob 不是「暫時沒東西」，
    是規則指錯樹了。
    """

    def _glob_rules(self):
        return [r for line_rules in bump_docs._build_rules().values()
                for r in line_rules if r.get("file") == "__glob__"]

    def test_every_glob_rule_expands_to_at_least_one_file(self):
        glob_rules = self._glob_rules()

        # Anti-vacuity：glob 規則整批被刪掉時，這條測試不能因為沒東西可檢查
        # 就自動變綠（那正是它要抓的失敗模式的極端版）。
        assert len(glob_rules) >= 5, (
            f"_build_rules() 只剩 {len(glob_rules)} 條 glob 規則。"
            f"這條測試檢查的是 glob 展開結果，規則集空掉時它會失去意義。")

        empty = []
        for rule in glob_rules:
            expanded = bump_docs._expand_glob_rules([rule])
            # ⚠️ 不能只判斷 `not expanded`：展開到 0 個檔案時現在會產生一個
            # glob_collapsed sentinel（好讓 runtime gate 看得見），所以「回傳
            # 非空」不再代表「有展開到檔案」。照舊寫法這條會變成永遠綠。
            if not expanded or any(r.get("glob_collapsed") for r in expanded):
                empty.append(f"{rule['glob_dir']}/{rule['glob_pattern']} "
                             f"({rule['desc']})")

        assert not empty, (
            f"{len(empty)} 條 glob 規則展開到 0 個檔案——它們對所有 gate 都是"
            f"隱形的（不產生規則 → 沒有 SKIP / DEAD / MISSING 可看）：\n  "
            + "\n  ".join(empty)
            + "\n修法：把 glob_dir / glob_pattern 指到檔案現在真正住的地方"
              "（例如 JSX 已搬到 PORTAL_JSX_DIR）；若那棵樹整個不在了，整條刪掉。")

    def test_portal_jsx_globs_point_at_the_real_jsx_tree(self):
        """PORTAL_JSX_DIR 必須真的裝著 JSX——這是上面那條的具體化。

        `glob_dir` 指到一個存在但空的目錄時，展開一樣是 0；分開斷言是為了讓
        「常數指錯地方」有一行直接點名的錯誤訊息。
        """
        jsx_dir = bump_docs.REPO_ROOT / bump_docs.PORTAL_JSX_DIR
        assert jsx_dir.is_dir(), f"PORTAL_JSX_DIR 不存在：{bump_docs.PORTAL_JSX_DIR}"
        assert list(jsx_dir.glob("**/*.jsx")), (
            f"{bump_docs.PORTAL_JSX_DIR} 底下沒有任何 .jsx——"
            f"JSX 大概又搬家了，PORTAL_JSX_DIR 要跟著改。")


class TestRequireMatchDefaults:
    """`require_match` 的預設值依規則來源而定（hand-written ON / glob OFF）。"""

    def test_hand_written_rule_defaults_to_require_match(self):
        assert bump_docs._requires_match({"file": "README.md"}) is True

    def test_glob_expanded_rule_defaults_to_not_require_match(self):
        assert bump_docs._requires_match(
            {"file": "docs/a.md", "from_glob": True}) is False

    def test_explicit_value_wins_over_both_defaults(self, tmp_path, monkeypatch):
        """明示值壓過兩邊預設——而且要走**真的產得出來**的規則。

        原本這條手搓 `{"file": ..., "from_glob": True, "require_match": True}`
        餵給 _requires_match()。那個 dict 生產路徑造不出來：_expand_glob_rules()
        當時只複製固定五個 key，`require_match` 在展開時就被丟掉了。於是這條
        測試「證明」了一個實際上永遠不會發生的行為（#1407 F7）。
        現在改成從 __glob__ 規則展開，讓斷言跟生產路徑同一條。
        """
        assert bump_docs._requires_match(
            {"file": "README.md", "require_match": False}) is False

        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("no version\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        expanded = bump_docs._expand_glob_rules([{
            "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
            "desc": "probe in docs", "pattern": r"v\d+\.\d+\.\d+",
            "replacement": lambda v: f"v{v}",
            "require_match": True,
        }])
        assert expanded, "glob 應展開到 1 個檔案"
        assert all(r.get("from_glob") for r in expanded)
        assert all(bump_docs._requires_match(r) is True for r in expanded), (
            "glob 規則上明示的 require_match: True 在展開時被吃掉了——"
            "_expand_glob_rules() 必須整份複製 key，不是複製白名單。")

    def test_expand_glob_rules_marks_expanded_rules(self, tmp_path, monkeypatch):
        """`from_glob` 是預設值的依據，所以展開時一定要蓋上去。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        expanded = bump_docs._expand_glob_rules([{
            "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
            "desc": "probe in docs", "pattern": "x",
            "replacement": lambda v: "y",
        }])
        assert expanded and all(r.get("from_glob") for r in expanded)

    def test_unmatched_hand_written_rule_is_dead_without_opt_in(self, tmp_path,
                                                               monkeypatch):
        """核心行為改變：沒宣告 require_match 的手寫規則撈不到 → DEAD。

        改動前這裡是 ("OK", "no match (may already be updated)")，於是十幾條
        規則長年綠燈卻什麼都沒 bump（#1407）。
        """
        (tmp_path / "README.md").write_text("no version here\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([{
            "file": "README.md", "desc": "probe",
            "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"v{v}",
        }], "2.9.0", check_only=True)
        assert changes[0][0] == "DEAD", changes

    def test_unmatched_glob_rule_stays_ok(self, tmp_path, monkeypatch):
        """反向護欄：glob 展開的規則撈不到仍是 OK，否則整個 docs/ 樹會爆紅。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("no version here\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([{
            "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
            "desc": "probe in docs",
            "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"v{v}",
        }], "2.9.0", check_only=True)
        assert changes[0][0] == "OK", changes


class TestTenantApiVersionLine:
    """tenant-api 版號線必須真的被評估（#1407 D-4）。"""

    def test_read_current_versions_includes_tenant_api(self):
        versions = bump_docs.read_current_versions()
        assert "tenant-api" in versions, (
            "read_current_versions() 沒讀 tenant-api——少了它，--check 迭代"
            "versions.items() 時整條線的規則永遠不會被執行。")

    def test_tenant_api_version_comes_from_chart_version_not_appversion(self):
        """SoT 是 Chart.yaml `version:`，不是 `appVersion:`。

        release.yaml 的 release-tenant-api job 用 `version:` 對 git tag 做
        gate；`appVersion` 是「最後發布的 binary」，刻意與 version 解耦
        （見 release.yaml 的 ⚠️ Verify image digest 註解）。兩者取錯會讓
        --check 拿 binary 版號去改 chart 版號、或反過來。
        """
        chart = (bump_docs.REPO_ROOT / "helm" / "tenant-api" / "Chart.yaml"
                 ).read_text(encoding="utf-8")
        import re as _re
        chart_ver = _re.search(r'^version:\s*"?([0-9.]+)"?', chart, _re.MULTILINE)
        app_ver = _re.search(r'^appVersion:\s*"?([0-9.]+)"?', chart, _re.MULTILINE)
        assert bump_docs.read_current_versions()["tenant-api"] == chart_ver.group(1)
        if app_ver and app_ver.group(1) != chart_ver.group(1):
            assert bump_docs.read_current_versions()["tenant-api"] != app_ver.group(1)

    def test_no_rule_writes_tenant_api_chart_appversion(self):
        """appVersion 的解耦是 release 不變量，不能有規則去改它。"""
        offenders = [r["desc"] for r in bump_docs._build_tenant_api_rules()
                     if r.get("file") == "helm/tenant-api/Chart.yaml"
                     and "appVersion" in r["pattern"]]
        assert not offenders, (
            f"這些規則會覆寫 tenant-api 的 Chart.yaml appVersion：{offenders}。"
            f"appVersion 追的是最後發布的 binary，與 chart version 刻意解耦"
            f"（release.yaml L3 digest 檢查會去 probe :v${{appVersion}}）。")


class TestCountRulesAreLive:
    """`--sync-counts` 也是 CI gate（validate.yaml），規則不能是死的。

    D-5：apply_count_updates() 過去對「檔案不存在」回 SKIP、對「撈不到」回 OK，
    而 --sync-counts 只看 UPDATE 決定 exit code——於是 7 條指著 CLAUDE.md 舊句型
    的規則印成 ✅ 綠勾，計數整批停止同步也沒人知道。
    """

    def test_count_rule_ids_are_pinned(self):
        """規則集是釘死的期望值，不是「這次剛好建出幾條」。

        `>= 3` 這種下限看不見五條掉一條：把 tool-registry.yaml 搬走時，
        JSX 那條規則整條消失，剩四條仍然 >= 3，`--sync-counts --check`
        exit 0，而 dev-rules.md 的數字繼續錯著（#1407 F2）。
        """
        ids = tuple(r["id"] for r in bump_docs._build_count_rules())
        assert ids == bump_docs.COUNT_RULE_IDS, (
            f"count 規則集與 COUNT_RULE_IDS 不符：{ids}。"
            f"增刪 count 規則時兩邊要同一筆改動一起改。")

    def test_every_count_rule_file_exists_and_pattern_matches(self):
        import re as _re
        dead, missing = [], []
        for rule in bump_docs._build_count_rules():
            fpath = bump_docs.REPO_ROOT / rule["file"]
            if not fpath.exists():
                missing.append(f"{rule['file']} ({rule['desc']})")
                continue
            content = fpath.read_text(encoding="utf-8")
            if not _re.findall(rule["pattern"], content, _re.MULTILINE):
                dead.append(f"{rule['file']} ({rule['desc']}): {rule['pattern']}")

        assert not missing, (
            f"{len(missing)} 條計數規則指向不存在的檔案：\n  " + "\n  ".join(missing))
        assert not dead, (
            f"{len(dead)} 條計數規則撈不到任何東西——這些計數已停止同步：\n  "
            + "\n  ".join(dead)
            + "\n修法：句型變了就改 pattern；句子真的沒了就整條刪掉"
              "（別為了餵規則把句子硬塞回文件）。")

    def test_missing_count_file_is_reported_as_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "_build_count_rules", lambda: [{
            "file": "gone.md", "desc": "probe", "pattern": r"\d+ things",
            "replacement": lambda _: "3 things", "is_count": True,
        }])
        changes = bump_docs.apply_count_updates(check_only=True)
        assert changes[0][0] == "MISSING", changes

    def test_unmatched_count_rule_is_reported_as_dead(self, tmp_path, monkeypatch):
        (tmp_path / "doc.md").write_text("nothing countable\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "_build_count_rules", lambda: [{
            "file": "doc.md", "desc": "probe", "pattern": r"\d+ things",
            "replacement": lambda _: "3 things", "is_count": True,
        }])
        changes = bump_docs.apply_count_updates(check_only=True)
        assert changes[0][0] == "DEAD", changes

    def test_missing_and_dead_stay_distinguishable(self, tmp_path, monkeypatch):
        """兩種診斷的修法不同（改 "file" vs 改 "pattern"），標籤不可合併。"""
        (tmp_path / "doc.md").write_text("nothing countable\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "_build_count_rules", lambda: [
            {"file": "gone.md", "desc": "m", "pattern": r"\d+ things",
             "replacement": lambda _: "3 things", "is_count": True},
            {"file": "doc.md", "desc": "d", "pattern": r"\d+ things",
             "replacement": lambda _: "3 things", "is_count": True},
        ])
        statuses = [c[0] for c in bump_docs.apply_count_updates(check_only=True)]
        assert statuses == ["MISSING", "DEAD"], statuses


class TestCheckWithExplicitVersionFlags:
    """`--check --<line> X` 必須真的檢查 X，而不是安靜忽略旗標。

    #1407 D-4 的第二個 bug：main() 裡「要不要走 bare-check 分支」的判斷列了
    platform/exporter/tools/portal/recipe-preview 五條線，獨漏 tenant_api。
    於是 `--check --tenant-api 9.9.9` 掉進 bare 分支——那個分支重新去讀
    **現況**版號，所以它檢查的是 2.9.20 而不是 9.9.9，結果 exit 0，指令看起來
    通過了但根本沒檢查使用者要求的版號。

    這條測試走真的 CLI（subprocess），因為 bug 就住在 argparse 之後的
    分支條件裡，import 進來呼叫函式是看不到的。
    """

    _SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "dx" / "bump_docs.py"

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self._SCRIPT), *args],
            capture_output=True, text=True,
        )

    @pytest.mark.parametrize("flag", [
        "--platform", "--exporter", "--tools",
        "--portal", "--recipe-preview", "--tenant-api",
    ])
    def test_check_with_absurd_version_fails_for_every_line(self, flag):
        """六條線一視同仁：沒有一條可以吞掉旗標。

        參數化到全部六條，而不是只補 tenant-api——漏一條正是原本的 bug 形狀，
        下次新增第七條版號線時這裡會逼人一起想到。
        """
        result = self._run("--check", flag, "9.9.9")
        assert result.returncode != 0, (
            f"`--check {flag} 9.9.9` exit 0——repo 裡不可能所有引用都是 9.9.9，"
            f"所以這個旗標被安靜忽略了（掉進 bare-check 分支）。\n"
            f"stdout tail:\n{result.stdout[-800:]}")


class TestVersionLineExpectation:
    """六條版號線是**釘死的期望值**，不是「這次剛好讀到幾條」（#1407 第二輪）。

    原本每個 consumer 都在 iterate `read_current_versions()` 的結果，於是
    「某條線的 SSOT 讀不到」不是錯誤，是那條線**不存在**——連同它底下所有規則
    一起從 gate 的視野裡消失。實測：把 CLAUDE.md:51 的一句話換個講法（版號
    數字不變、只改措辭），platform 線與它的 2170 條規則一起蒸發，而
    `--check` 印「✅ All version references are consistent.」exit 0、
    `--what-if` 印「Summary: 670 rules, 670 ✅, 0 DEAD, 0 MISSING」exit 0。

    D-4 修掉的 tenant-api 是這個 class 的一個實例；這裡釘的是 class 本身。
    """

    def test_version_lines_matches_build_rules_keys(self):
        """VERSION_LINES 與 _build_rules() 的 key 必須是同一組。

        兩邊分家就是漏洞重生：宣告了線卻沒有規則（永遠 vacuous 通過），或
        有規則卻沒宣告線（那條線不會被 iterate 到）。
        """
        assert set(bump_docs.VERSION_LINES) == set(bump_docs._build_rules()), (
            "VERSION_LINES 與 _build_rules() 的版號線不一致——新增版號線要同時"
            "改 VERSION_LINE_SOURCES、_build_rules() 與 read_current_versions()。")

    def test_missing_version_lines_reports_unreadable_line(self, tmp_path,
                                                           monkeypatch):
        """SSOT 讀不到 → 出現在 missing_version_lines()，而不是安靜消失。"""
        # REPO_ROOT 指到空目錄 → CLAUDE.md 不在 → platform 讀不到；
        # 其餘五條的 SSOT 是模組層絕對路徑，仍讀真 repo。
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        missing = dict((line, src) for line, src, _ in
                       bump_docs.missing_version_lines())
        assert "platform" in missing
        assert missing["platform"] == "CLAUDE.md"

    def test_reworded_claude_md_is_reported_not_dropped(self, tmp_path,
                                                        monkeypatch):
        """審查者的原始 repro：同一個版號、換句話說，platform 線不可以消失。"""
        (tmp_path / "CLAUDE.md").write_text(
            "## 專案概覽\n\n**Dynamic Alerting 多租戶平台 v2.9.0** — …\n",
            encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        versions = bump_docs.read_current_versions()
        assert "platform" not in versions          # 確實讀不到（前提成立）
        assert any(line == "platform"
                   for line, _, _ in bump_docs.missing_version_lines(versions)), (
            "CLAUDE.md 換了句型之後，platform 必須被報成 NO-SSOT——"
            "而不是變成「沒有 platform 這條線」。")

    def test_check_fails_when_a_line_has_no_ssot(self, monkeypatch, capsys,
                                                 cli_argv):
        """`--check` 遇到讀不到的線要 exit 1 並印 NO-SSOT。"""
        real = bump_docs.read_current_versions()
        monkeypatch.setattr(bump_docs, "read_current_versions",
                            lambda: {k: v for k, v in real.items()
                                     if k != "platform"})
        cli_argv("bump_docs", "--check")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0, (
            "少了一條版號線時 --check 仍 exit 0——那條線的規則一條都沒跑，"
            "「一致」是在講它有看的那些。")
        assert "NO-SSOT" in out and "platform" in out
        assert "All version references are consistent" not in out

    def test_what_if_fails_when_a_line_has_no_ssot(self, monkeypatch, capsys,
                                                   cli_argv):
        """`--what-if` 同理：⚠️ 一行然後 exit 0 是不夠的。"""
        real = bump_docs.read_current_versions()
        monkeypatch.setattr(bump_docs, "read_current_versions",
                            lambda: {k: v for k, v in real.items()
                                     if k != "tenant-api"})
        cli_argv("bump_docs", "--what-if")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0
        assert "NO-SSOT" in out
        assert "1 ❌ NO-SSOT" in out, "Summary 必須把 NO-SSOT 數出來"

    def test_show_current_fails_when_a_line_has_no_ssot(self, monkeypatch,
                                                        capsys, cli_argv):
        """--show-current 是這個 bug 的第一現場：少一行看起來跟健康一樣。"""
        real = bump_docs.read_current_versions()
        monkeypatch.setattr(bump_docs, "read_current_versions",
                            lambda: {k: v for k, v in real.items()
                                     if k != "portal"})
        cli_argv("bump_docs", "--show-current")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0
        assert "portal: ❌ NOT FOUND" in out


class TestPrereleaseVersions:
    """release candidate 不可以讓某條線靜靜消失，也不可以被寫成 `-rc1-rc1`。

    `_SEMVER_STRICT`（無 suffix）曾同時用在 SSOT 讀取與 image tag / chart
    version 的寫入規則上。讀的一邊：`appVersion: "2.10.0-rc1"` 比對不到 →
    exporter 線消失（就是上面那個 class 的 bug，只是觸發原因是一次合法的
    rc bump）。寫的一邊更糟：strict pattern 會咬到 `2.10.0-rc1` 的 `2.10.0`
    前綴，永遠比不上 replacement → --check 永遠 drift，而真的寫下去會變成
    `2.10.0-rc1-rc1`。
    """

    def test_chart_ssot_accepts_prerelease(self, tmp_path, monkeypatch):
        chart = tmp_path / "Chart.yaml"
        chart.write_text('apiVersion: v2\nname: threshold-exporter\n'
                         'version: 2.10.0-rc1\nappVersion: "2.10.0-rc1"\n',
                         encoding="utf-8")
        monkeypatch.setattr(bump_docs, "CHART_YAML", chart)
        assert bump_docs.read_current_versions().get("exporter") == "2.10.0-rc1"

    def test_version_file_ssot_accepts_prerelease(self, tmp_path, monkeypatch):
        vf = tmp_path / "VERSION"
        vf.write_text("2.10.0-rc1\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "DA_TOOLS_VERSION", vf)
        assert bump_docs.read_current_versions().get("tools") == "2.10.0-rc1"

    def test_rule_patterns_are_idempotent_on_prerelease_versions(self):
        """沒有任何規則的 pattern 可以只咬中自己輸出的**前綴**。

        這是 `-rc1-rc1` 那個 corruption 的機械化檢查：把 replacement 產出來
        當輸入餵回自己的 pattern，撈到的東西若是輸出的真前綴，就代表再跑一次
        會再接一次 suffix。撈不到（靠 lookahead/lookbehind 的上下文不在）
        則跳過——這條只負責證偽，不假裝證實。
        """
        offenders = []
        for line, rules in bump_docs._build_rules().items():
            for rule in rules:
                if rule.get("whole_file") or rule.get("pair_anchor"):
                    continue
                repl = rule["replacement"]("9.9.9-rc1")
                text = f"\n{repl}\n"
                for m in re.findall(rule["pattern"], text, re.MULTILINE):
                    if isinstance(m, tuple):        # capturing groups
                        continue
                    if m != repl and repl.startswith(m):
                        offenders.append(
                            f"[{line}] {rule['desc']}: pattern 咬到 {m!r}，"
                            f"但自己的輸出是 {repl!r}")
        assert not offenders, (
            "以下規則對 pre-release 版號不是冪等的——再跑一次會把 suffix 再接"
            "一次（2.10.0-rc1-rc1），而 --check 會永遠報 drift：\n  "
            + "\n  ".join(offenders))


class TestGlobGroupHealth:
    """glob 的健康條件是「整棵展開樹裡至少撈到一次」，不是「展開到 >=1 個檔案」。

    per-file 的 require_match 對 glob 是關的（正確：一棵樹裡多數檔案本來就
    沒有那個字串），所以整條 glob 從頭到尾撈不到時，每個檔案都印 ✅ 綠勾，
    沒有任何一層看得見。實測有三條規則長期處於這個狀態，其中
    `**最後更新**` 那條因為 pattern 帶了 `(?=\\s*\\|)` lookahead 而永遠撈不到，
    docs/internal/design-system-guide.md 的頁尾就這樣停在 v2.6.0。

    ⛔ 兩個診斷刻意分開，因為修法不同：
      GLOB-EMPTY  展開到 0 個檔案 → 改 glob_dir / glob_pattern。
      GLOB-DEAD   展開到檔案但整棵樹 0 命中 → 改 pattern，或整條刪掉。
    而且兩者都在 runtime 判定：`make pre-tag` 不跑 pytest，只有測試看得到的
    守門員對 release gate 而言等於不存在（#1407 F6）。
    """

    _GLOB = {
        "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
        "desc": "probe in docs",
        "pattern": r"\*\*最後更新\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**最後更新**：v{v}",
    }

    def _docs(self, tmp_path, **files):
        (tmp_path / "docs").mkdir()
        for name, body in files.items():
            (tmp_path / "docs" / f"{name}.md").write_text(body, encoding="utf-8")
        return tmp_path

    def test_glob_matching_nothing_anywhere_is_glob_dead(self, tmp_path,
                                                         monkeypatch):
        self._docs(tmp_path, a="# A\n沒有頁尾\n", b="# B\n也沒有\n")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([dict(self._GLOB)], "2.9.0",
                                        check_only=True)
        statuses = [c[0] for c in changes]
        assert "GLOB-DEAD" in statuses, (
            f"整棵樹一個都沒撈到卻沒有 GLOB-DEAD：{statuses}")
        # 反面同時釘住：per-file 仍然是 OK（不能靠把 glob 的 require_match
        # 打開來「解決」——那會讓整個 docs/ 樹爆紅）。
        assert statuses.count("OK") == 2

    def test_glob_matching_once_is_healthy(self, tmp_path, monkeypatch):
        self._docs(tmp_path,
                   a="# A\n沒有頁尾\n",
                   b="# B\n\n**最後更新**：v2.9.0\n")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        statuses = [c[0] for c in bump_docs.apply_rules(
            [dict(self._GLOB)], "2.9.0", check_only=True)]
        assert "GLOB-DEAD" not in statuses, (
            "一棵樹裡命中一次就是活的——把門檻抬到「每個檔案都要命中」會讓"
            "所有 docs glob 永遠紅。")

    def test_collapsed_glob_is_glob_empty_at_runtime(self, tmp_path,
                                                     monkeypatch):
        (tmp_path / "docs").mkdir()          # 空目錄：展開到 0 個檔案
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([dict(self._GLOB)], "2.9.0",
                                        check_only=True)
        statuses = [c[0] for c in changes]
        assert statuses == ["GLOB-EMPTY"], (
            f"glob 塌成 0 個檔案時 runtime 必須出聲（不是只有 pytest 抓得到）："
            f"{changes}")

    def test_collapsed_glob_is_not_double_reported(self, tmp_path, monkeypatch):
        """一個缺陷一個診斷：GLOB-EMPTY 不應該再附送一條 GLOB-DEAD。"""
        (tmp_path / "docs").mkdir()
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        statuses = [c[0] for c in bump_docs.apply_rules(
            [dict(self._GLOB)], "2.9.0", check_only=True)]
        assert statuses.count("GLOB-DEAD") == 0

    def test_expansion_carries_glob_id(self, tmp_path, monkeypatch):
        """group 判定靠 glob_id；展開時沒蓋上去的話整個機制是死的。"""
        self._docs(tmp_path, a="# A\n", b="# B\n")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        expanded = bump_docs._expand_glob_rules([dict(self._GLOB)])
        assert len(expanded) == 2
        assert len({r["glob_id"] for r in expanded}) == 1

    @pytest.mark.parametrize("line", bump_docs.VERSION_LINES)
    def test_live_repo_has_no_dead_or_collapsed_glob(self, line):
        """今天的 repo：每一條 glob 都必須在自己的樹裡至少撈到一次。

        這是三個 rot 點的回歸鎖（`**最後更新**` lookahead、`> **vX.Y.Z**`
        無管線、tenant-api image tag in docs）。
        """
        version = bump_docs.read_current_versions()[line]
        rules = bump_docs._build_rules()[line]
        bad = [(c[0], c[1]) for c in
               bump_docs.apply_rules(rules, version, check_only=True)
               if c[0] in ("GLOB-DEAD", "GLOB-EMPTY")]
        assert not bad, (
            f"{line} 有 glob 規則什麼都沒撈到：\n  "
            + "\n  ".join(f"{s}: {d}" for s, d in bad))


    def test_check_cli_fails_on_a_dead_glob(self, tmp_path, monkeypatch,
                                            capsys, cli_argv):
        """接線層鎖：`--check`（= `make version-check`，pre-tag 的版號閘門）
        必須因為 GLOB-DEAD 而 exit 1。

        上面的單元測試證明 apply_rules() 會產生這個狀態；這條證明 main() 有
        接住它。少了接線，診斷只會印在畫面上而 gate 照樣綠——那正是 SKIP 在
        #1407 之前的處境。
        """
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("沒有任何版號\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "read_current_versions",
                            lambda: {line: "2.9.0"
                                     for line in bump_docs.VERSION_LINES})
        monkeypatch.setattr(bump_docs, "_build_rules", lambda: {
            line: ([dict(self._GLOB)] if line == "platform" else [])
            for line in bump_docs.VERSION_LINES})

        cli_argv("bump_docs", "--check")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0, (
            "整條 glob 撈不到時 `--check` 仍 exit 0——release gate 會說「版號"
            "全部一致」，其實那棵樹一個字都沒被驅動。")
        assert "GLOB-DEAD" in out
        assert "All version references are consistent" not in out

class TestExpandGlobPreservesEveryKey:
    """`_expand_glob_rules()` 不可以偷偷丟 key（#1407 F7）。

    它原本只複製固定五個 key，所以 glob 規則上設的 require_match /
    whole_file / pair_anchor / pair_key 會被無聲丟掉——而 apply_rules 的
    docstring 明寫「Opt OUT with an explicit `require_match: False`」。
    今天沒有 glob 規則設這些，但「設了沒作用而且不出聲」正是這整輪在拔的
    東西。
    """

    def test_require_match_true_survives_and_takes_effect(self, tmp_path,
                                                          monkeypatch):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("沒有版號\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        changes = bump_docs.apply_rules([{
            "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
            "desc": "probe in docs",
            "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+",
            "replacement": lambda v: f"v{v}",
            "require_match": True,
        }], "2.9.0", check_only=True)
        assert changes[0][0] == "DEAD", (
            f"glob 上明示的 require_match: True 沒有生效：{changes}")

    def test_skip_released_changelog_still_survives(self, tmp_path,
                                                    monkeypatch):
        """既有的 key 不能因為改成整份複製就掉了（反向護欄）。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        expanded = bump_docs._expand_glob_rules([{
            "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
            "desc": "probe in docs", "pattern": "x",
            "replacement": lambda v: "y",
            "skip_released_changelog": True,
        }])
        assert all(r["skip_released_changelog"] for r in expanded)

    def test_glob_bookkeeping_keys_are_not_leaked_as_file_rules(
            self, tmp_path, monkeypatch):
        """展開後的規則不能還帶著 glob_dir/glob_pattern（那是模板的東西）。"""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        expanded = bump_docs._expand_glob_rules([{
            "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
            "desc": "probe in docs", "pattern": "x",
            "replacement": lambda v: "y",
        }])
        assert all("glob_dir" not in r and "glob_pattern" not in r
                   for r in expanded)


class TestCountSourceUnreadable:
    """count 來源讀不到 → NO-SOURCE，而不是規則消失（#1407 F2）。"""

    def test_rule_survives_when_source_unreadable(self, tmp_path, monkeypatch):
        """tool-registry.yaml 不在 → JSX 規則仍在，只是 source_ok=False。"""
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        rules = {r["id"]: r for r in bump_docs._build_count_rules()}
        assert tuple(rules) == bump_docs.COUNT_RULE_IDS, (
            "來源讀不到時規則整條消失了——這正是 --sync-counts --check 會對著"
            "一個過期數字 exit 0 的原因。")
        assert rules["dev-rules-jsx-tools"]["source_ok"] is False

    def test_unreadable_source_is_reported_as_no_source(self, tmp_path,
                                                        monkeypatch):
        (tmp_path / "doc.md").write_text("專案有 **9 個 JSX 互動工具**\n",
                                         encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "_build_count_rules", lambda: [{
            "id": "probe", "file": "doc.md", "desc": "probe",
            "pattern": r"\d+ 個", "replacement": lambda _: "3 個",
            "is_count": True, "source": "docs/assets/tool-registry.yaml",
            "source_ok": False,
        }])
        changes = bump_docs.apply_count_updates(check_only=True)
        assert changes[0][0] == "NO-SOURCE", changes
        assert "tool-registry.yaml" in changes[0][2]

    def test_no_source_is_distinct_from_missing_and_dead(self, tmp_path,
                                                         monkeypatch):
        """三種診斷三種修法（修來源 / 修 file / 修 pattern），標籤不可合併。"""
        (tmp_path / "doc.md").write_text("nothing countable\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "_build_count_rules", lambda: [
            {"id": "a", "file": "doc.md", "desc": "n", "pattern": r"\d+ things",
             "replacement": lambda _: "3 things", "is_count": True,
             "source": "src.yaml", "source_ok": False},
            {"id": "b", "file": "gone.md", "desc": "m", "pattern": r"\d+ things",
             "replacement": lambda _: "3 things", "is_count": True,
             "source": "src.yaml", "source_ok": True},
            {"id": "c", "file": "doc.md", "desc": "d", "pattern": r"\d+ things",
             "replacement": lambda _: "3 things", "is_count": True,
             "source": "src.yaml", "source_ok": True},
        ])
        statuses = [c[0] for c in bump_docs.apply_count_updates(check_only=True)]
        assert statuses == ["NO-SOURCE", "MISSING", "DEAD"], statuses

    def test_sync_counts_check_fails_on_no_source(self, monkeypatch, capsys,
                                                  cli_argv):
        """CI 步驟（validate.yaml 的 Count consistency）必須因此變紅。"""
        monkeypatch.setattr(bump_docs, "_count_jsx_tools", lambda: 0)
        cli_argv("bump_docs", "--sync-counts", "--check")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0, (
            "tool-registry.yaml 讀不到時 --sync-counts --check 仍 exit 0——"
            "dev-rules.md 的數字會一直錯下去而沒人知道。")
        assert "NO-SOURCE" in out
        assert "All counts are already up to date" not in out

    def test_plain_sync_counts_also_fails_on_no_source(self, monkeypatch,
                                                       capsys, cli_argv,
                                                       tmp_path):
        """非 --check 模式也不可以報「Done, 0 updated」了事。"""
        monkeypatch.setattr(bump_docs, "_count_jsx_tools", lambda: 0)
        cli_argv("bump_docs", "--sync-counts", "--dry-run")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        assert exc.value.code != 0
        assert "NO-SOURCE" in capsys.readouterr().out


class TestScopeMustSelectSomething:
    """`--scope` 過濾到 0 條規則是 caller error，不是通過（#1407 F8）。"""

    _SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "dx" / "bump_docs.py"

    def _run(self, *args):
        return subprocess.run([sys.executable, str(self._SCRIPT), *args],
                              capture_output=True, text=True)

    def test_bogus_scope_is_caller_error_in_check(self):
        r = self._run("--check", "--scope", "nosuchdir")
        assert r.returncode == 2, (
            f"`--check --scope nosuchdir` 應為 caller error(2)，實得 "
            f"{r.returncode}。exit 0 表示「我一條規則都沒看」與「全部一致」"
            f"長得一模一樣。\nstdout:\n{r.stdout[-500:]}")
        assert "consistent" not in r.stdout

    def test_bogus_scope_is_caller_error_in_bump(self):
        r = self._run("--platform", "9.9.9", "--dry-run", "--scope", "nosuchdir")
        assert r.returncode == 2, r.stdout[-500:]

    def test_real_scope_still_passes(self):
        r = self._run("--check", "--scope", "docs")
        assert r.returncode == 0, r.stdout[-800:]


class TestTenantApiDockerfilePin:
    """components/tenant-api/Dockerfile 的 image pin 必須被規則驅動（#1407 F9）。

    那兩行 `docker build -t` / `docker run` 的註解停在 :2.4.0，chart 已經
    2.9.20——五個 minor 的落差，而且就在使用者直接複製貼上的指令裡。原本沒有
    任何規則涵蓋它：唯一相關的 glob 只看 docs/（而且那條 glob 本身 0 命中）。
    """

    def _rule(self):
        rules = [r for r in bump_docs._build_tenant_api_rules()
                 if r.get("file") == "components/tenant-api/Dockerfile"
                 and "image tag" in r["desc"]]
        assert len(rules) == 1, f"預期剛好一條 Dockerfile image-tag 規則：{rules}"
        return rules[0]

    def test_rule_exists_and_requires_match(self):
        assert self._rule().get("require_match") is True

    def test_rule_is_live_against_the_real_dockerfile(self):
        version = bump_docs.read_current_versions()["tenant-api"]
        changes = bump_docs.apply_rules([self._rule()], version,
                                        check_only=True)
        assert [c[0] for c in changes] == ["OK"], changes

    def test_dockerfile_pins_are_v_prefixed_and_current(self):
        """v 前綴不是美觀問題：release.yaml 推的是 `:v${version}`，
        沒有前綴的 tag 在 registry 裡不存在。"""
        version = bump_docs.read_current_versions()["tenant-api"]
        text = (bump_docs.REPO_ROOT / "components" / "tenant-api"
                / "Dockerfile").read_text(encoding="utf-8")
        pins = re.findall(r"ghcr\.io/vencil/tenant-api:\S+", text)
        assert pins, "Dockerfile 裡的 image pin 不見了——規則會 DEAD"
        assert all(p == f"ghcr.io/vencil/tenant-api:v{version}" for p in pins), pins



class TestReleaseLineTableRows:
    """da-tools 版號表的 tenant-api 列 + QUICKSTART 的 portal image pin（#1407 第三輪 F1）。

    這兩處是「沒有任何規則涵蓋」的真陳舊：
      components/da-tools/README.md   `| tenant-api | v2.8.0 | \\`tenant-api/v2.8.0\\` |`
                                      ——該表五列裡唯一沒有專屬規則的一列。
      components/da-portal/QUICKSTART.md `docker run … da-portal:v2.8.0`
                                      ——2 分鐘上手路徑真的會被複製貼上的那行。

    ⛔ 反面同樣重要：repo 內另外三處 `tenant-api:v2.7.0`（k8s deployment /
    docs/assets/platform-data.json / portal images.js）**是對的**，不可以順手
    「修好」。tenant-api 的 image tag 刻意追 Chart.yaml 的 `appVersion`（最後
    發布的 binary），chart 部署 `:v${appVersion}`、release.yaml 的 L3 digest
    step 也是探那個 tag；而本表列的是 **release line**（它直接寫出 git tag
    `tenant-api/vX.Y.Z`），追的是 `version:`。platform-data.json 更是
    generate_platform_data.py 產生的，加規則會跟產生器打架。
    """

    def _rule(self, builder, desc):
        rules = [r for r in builder() if r.get("desc") == desc]
        assert len(rules) == 1, f"預期剛好一條 {desc!r} 規則：{rules}"
        return rules[0]

    def _tenant_api_row(self):
        return self._rule(bump_docs._build_tenant_api_rules,
                          "tenant-api version + git tag in da-tools strategy table")

    def _portal_quickstart(self):
        return self._rule(bump_docs._build_portal_rules,
                          "da-portal image tag in QUICKSTART")

    def test_both_rules_require_match(self):
        """兩條都是 hand-written 且顯式 require_match：句型再被改寫時要大聲死掉，
        不可以靜靜退回「沒人涵蓋」的狀態——那正是它們的成因。"""
        assert self._tenant_api_row().get("require_match") is True
        assert self._portal_quickstart().get("require_match") is True

    def test_tenant_api_row_is_live_and_current(self):
        version = bump_docs.read_current_versions()["tenant-api"]
        changes = bump_docs.apply_rules([self._tenant_api_row()], version,
                                        check_only=True)
        assert [c[0] for c in changes] == ["OK"], changes

    def test_portal_quickstart_is_live_and_current(self):
        version = bump_docs.read_current_versions()["portal"]
        changes = bump_docs.apply_rules([self._portal_quickstart()], version,
                                        check_only=True)
        assert [c[0] for c in changes] == ["OK"], changes

    def test_tenant_api_row_tracks_chart_version_not_appversion(self):
        """該列的數字必須等於 Chart.yaml 的 `version:`，而不是 `appVersion:`。

        兩者在這個 chart 上刻意脫鉤，抓錯來源就會把 release-line 引用寫成
        image-runtime pin（或反之）。"""
        chart = bump_docs.TENANT_API_CHART_YAML.read_text(encoding="utf-8")
        chart_version = re.search(r'^version:\s*"?([^"\s]+)"?', chart,
                                  re.MULTILINE).group(1)
        app_version = re.search(r'^appVersion:\s*"?([^"\s]+)"?', chart,
                                re.MULTILINE).group(1)
        assert chart_version != app_version, (
            "前提沒了：這條測試的意義建立在兩者脫鉤上")

        readme = (bump_docs.REPO_ROOT / "components" / "da-tools"
                  / "README.md").read_text(encoding="utf-8")
        row = re.search(r"\| tenant-api \| v(\S+) \| `tenant-api/v(\S+)`", readme)
        assert row, "da-tools README 的 tenant-api 列不見了——規則會 DEAD"
        assert row.group(1) == chart_version, (row.group(1), chart_version)
        assert row.group(2) == chart_version, (row.group(2), chart_version)

    def test_image_runtime_pins_are_left_alone(self):
        """沒有任何規則會去動那三處刻意追 appVersion 的 image pin。"""
        chart = bump_docs.TENANT_API_CHART_YAML.read_text(encoding="utf-8")
        app_version = re.search(r'^appVersion:\s*"?([^"\s]+)"?', chart,
                                re.MULTILINE).group(1)
        targets = ["k8s/04-tenant-api/deployment.yaml",
                   "docs/assets/platform-data.json",
                   "tools/portal/src/interactive/tools/_common/data/images.js"]
        for rel in targets:
            path = bump_docs.REPO_ROOT / rel
            if not path.exists():        # 檔案搬走了不是本測試要管的事
                continue
            pins = re.findall(r"tenant-api:v?(\d+\.\d+\.\d+)",
                              path.read_text(encoding="utf-8"))
            assert pins, f"{rel} 的 tenant-api pin 不見了"
            assert all(p == app_version for p in pins), (
                f"{rel} 的 image pin {pins} 應該追 appVersion {app_version}；"
                f"若有人替它加了 bump 規則，這裡會先紅")

        covered = {r.get("file") for r in bump_docs._build_rules()["tenant-api"]}
        assert covered.isdisjoint(targets), (
            f"這三個檔案是 appVersion-tracking 的 image pin，不該有 tenant-api "
            f"版號線規則：{covered & set(targets)}")


class TestChangelogReachingGlobsSkipFrozenHistory:
    """能碰到 docs/CHANGELOG.md 的 glob 一律要 skip_released_changelog（#1407 第三輪 F3）。

    docs/CHANGELOG.md 是 root CHANGELOG.md 的 **symlink**，所以它是每一條
    `docs/**/*.md` glob 展開結果的成員——glob 規則沒有辦法「不看到它」。已發布
    的 `## [vX.Y.Z]` 區段是凍結歷史：裡面的版號是「那一版做了什麼」的事實，
    不是指向現行版本的指標，改掉就是竄改歷史，而且會被報成一般的 UPDATE。

    原本只有 `於 v` 那條帶旗標（因為 PR #503 燒過）。這個 branch 又把三個活的
    pattern 廣播到同一批檔案上（`**文件版本：**` / `**Document version:**` /
    `**最後更新**：`，最後一條先前只是因為 `(?=\\s*\\|)` lookahead 才沒作用，
    而那個 lookahead 在本 branch 被拿掉了），三條都沒有旗標。

    這裡的不變式**從 glob 展開結果推導**，不維護白名單——新加一條 docs glob
    而忘記旗標時，這條會自己紅。
    """

    def _changelog_reaching_rules(self):
        out = []
        for line, rules in bump_docs._build_rules().items():
            for rule in rules:
                if rule.get("file") != "__glob__":
                    continue
                files = {e["file"]
                         for e in bump_docs._expand_glob_rules([dict(rule)])}
                if "docs/CHANGELOG.md" in files:
                    out.append((line, rule))
        return out

    def test_the_invariant_has_teeth(self):
        """前提檢查：真的有 glob 碰得到 CHANGELOG。否則下一條是空跑。"""
        assert len(self._changelog_reaching_rules()) >= 6, (
            "沒有任何 glob 展開到 docs/CHANGELOG.md——symlink 或 glob 換了形狀，"
            "這組測試已失去意義，請重新確認凍結歷史還有沒有被保護")

    def test_every_changelog_reaching_glob_skips_frozen_history(self):
        offenders = [f"[{line}] {rule['desc']}"
                     for line, rule in self._changelog_reaching_rules()
                     if not rule.get("skip_released_changelog")]
        assert not offenders, (
            "以下 glob 規則展開後含 docs/CHANGELOG.md 卻沒有 "
            "skip_released_changelog——它們可以改寫已發布的 `## [vX.Y.Z]` 區段，"
            "而且會報成一般 UPDATE（PR #503 那一類）：\n  "
            + "\n  ".join(offenders))

    @pytest.mark.parametrize("desc,line,body,frozen_line", [
        ("doc footer **文件版本：** vX.Y.Z", "platform",
         "**文件版本：** v0.1.0", "**文件版本：** v0.1.0"),
        ("doc footer **Document version:** vX.Y.Z", "platform",
         "**Document version:** v0.1.0", "**Document version:** v0.1.0"),
        ("doc footer **最後更新**：vX.Y.Z pattern", "platform",
         "**最後更新**：v0.1.0", "**最後更新**：v0.1.0"),
    ])
    def test_footer_inside_frozen_entry_is_not_rewritten(
            self, desc, line, body, frozen_line, tmp_path, monkeypatch):
        """本 branch 新加的三個 pattern：在凍結區段裡插一行頁尾，bump 後必須原封不動。

        （這正是 review 用來示範的手法：把頁尾插進 `## [v0.1.0]` 之後再跑
        `--platform`，舊版會改寫它並報 UPDATE。）
        """
        rule = [r for r in bump_docs._build_rules()[line]
                if r.get("desc") == desc]
        assert len(rule) == 1, desc
        rule = rule[0]

        docs = tmp_path / "docs"
        docs.mkdir()
        changelog = docs / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n- 待發佈。\n\n"
            f"## [v0.1.0] — synthetic (2026-01-01)\n\n- 條目。\n\n{frozen_line}\n",
            encoding="utf-8")
        os.chmod(changelog,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

        changes = bump_docs.apply_rules([dict(rule)], "0.2.0", check_only=False)

        after = changelog.read_text(encoding="utf-8")
        assert frozen_line in after, (
            f"凍結區段裡的 {frozen_line!r} 被改寫了：\n{after}")
        assert "0.2.0" not in after, after
        assert "UPDATE" not in [c[0] for c in changes], changes

    def test_live_section_of_changelog_still_bumped(self, tmp_path, monkeypatch):
        """反面：旗標不可以連 `## [Unreleased]` 上方的活內容一起跳過。"""
        rule = [r for r in bump_docs._build_rules()["platform"]
                if r.get("desc") == "doc footer **文件版本：** vX.Y.Z"][0]
        docs = tmp_path / "docs"
        docs.mkdir()
        changelog = docs / "CHANGELOG.md"
        changelog.write_text(
            "# Changelog\n\n**文件版本：** v0.1.0\n\n"
            "## [Unreleased]\n\n- 待發佈。\n\n"
            "## [v0.1.0] — synthetic (2026-01-01)\n\n- 條目。\n",
            encoding="utf-8")
        os.chmod(changelog,
                 stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

        bump_docs.apply_rules([dict(rule)], "0.2.0", check_only=False)
        assert "**文件版本：** v0.2.0" in changelog.read_text(encoding="utf-8")


class TestPerLineScopeGuard:
    """`--scope` 的空集合檢查必須是 per-line，不是全 repo 加總（#1407 第三輪 F2）。

    舊版把 filter 套在全部六條線上加總，但 bump / check 迴圈是**逐線**過濾的。
    於是「全域非空、但對正在處理的那條線是空的」scope 會通過守門員，然後一條
    規則都沒評估就報成功——正是守門員 docstring 自己說要擋掉的那件事：

        --tenant-api 9.9.9 --scope docs         → ✅ Done. 0 update(s) applied.  exit 0
        --check --tenant-api 9.9.9 --scope docs → ✅ … already up to date.        exit 0
    """

    _SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "tools"
               / "dx" / "bump_docs.py")

    def _run(self, *args):
        return subprocess.run([sys.executable, str(self._SCRIPT), *args],
                              capture_output=True, text=True)

    def test_bump_with_line_empty_scope_is_caller_error(self):
        r = self._run("--tenant-api", "9.9.9", "--scope", "docs", "--dry-run")
        assert r.returncode == 2, (
            f"`--tenant-api 9.9.9 --scope docs` 應為 caller error(2)，實得 "
            f"{r.returncode}。exit 0 表示「我一條 tenant-api 規則都沒評估」"
            f"與「已完成」長得一樣。\nstdout:\n{r.stdout[-600:]}")
        assert "tenant-api" in r.stderr
        assert "Done" not in r.stdout

    def test_check_with_line_empty_scope_is_caller_error(self):
        r = self._run("--check", "--tenant-api", "9.9.9", "--scope", "docs")
        assert r.returncode == 2, r.stdout[-600:]
        assert "up to date" not in r.stdout

    def test_guard_runs_before_any_write(self):
        """守門員在迴圈**之前**跑：多線 bump 時不可以先寫好一條線再失敗。

        platform 在 docs 下有 ~2100 條規則、tenant-api 一條都沒有。若守門員
        擺在迴圈內，這個指令會先把整棵 docs/ 處理完才發現 tenant-api 沒東西可
        做——在沒有 --check 的那條路徑上，那就是已經寫進去了。

        ⛔ 這條**故意**用 `--check` 跑真實 repo。要證的是「守門員在迴圈之前
        fire」，而 stdout 裡有沒有 `PLATFORM` 區塊標題就是那個順序的證據；把
        `--check` 拿掉雖然更貼近災難現場，但一旦守門員退化，測試自己就會把整
        棵 docs/ 改寫成 9.9.9（實測會弄髒 237 個檔案）。測試不該有那種爆炸半徑。
        """
        r = self._run("--check", "--platform", "9.9.9", "--tenant-api",
                      "9.9.9", "--scope", "docs")
        assert r.returncode == 2, r.stdout[-600:]
        assert "PLATFORM" not in r.stdout, (
            "守門員在迴圈裡才 fire：platform 線已經整個跑完了，換成不帶 "
            f"--check 的同一個指令就是 2100 條規則落地。\n{r.stdout[-600:]}")

    def test_line_with_rules_under_scope_still_works(self):
        """反面：scope 對該線非空時照常運作，守門員不得誤傷。"""
        r = self._run("--platform", "9.9.9", "--scope", "docs", "--dry-run")
        assert r.returncode == 0, (r.returncode, r.stdout[-600:], r.stderr[-400:])

    def test_bare_check_reports_scope_excluded_lines(self):
        """bare `--check --scope helm` 不再靜靜跳過 platform 線。

        這裡刻意**不**要求 exit 2：bare check 沒有指名任何一條線，`--scope helm`
        的語意就是「只看 helm」，而 platform 在 helm 下本來就沒有規則。錯的是
        「完全不吭聲」，不是 exit code——所以修法是把它列印成一個獨立診斷
        SCOPE-EMPTY，與 NO-SSOT / MISSING / DEAD / GLOB-* 分開標籤。
        """
        r = self._run("--check", "--scope", "helm")
        assert "SCOPE-EMPTY" in r.stdout, r.stdout[-600:]
        assert "[platform]" in r.stdout, r.stdout[-600:]
        assert r.returncode == 0, r.stdout[-600:]

    def test_repo_wide_zero_scope_still_caller_error(self):
        """不得弱化：全域 0 條規則仍然是 exit 2。"""
        r = self._run("--check", "--scope", "nosuchdir")
        assert r.returncode == 2, r.stdout[-600:]

    def test_what_if_reports_scope_excluded_lines(self):
        r = self._run("--what-if", "--scope", "helm")
        assert "SCOPE-EMPTY" in r.stdout, r.stdout[-800:]

    def test_helper_is_pure_per_line(self):
        """單元層：全域非空、目標線為空 → 一定要 exit 2。"""
        all_rules = {"a": [{"file": "docs/x.md"}], "b": [{"file": "helm/y.yaml"}]}
        with pytest.raises(SystemExit) as exc:
            bump_docs._require_nonempty_line_scope(all_rules, "docs", ["b"])
        assert exc.value.code == 2
        # 目標線非空 → 不得拋出
        bump_docs._require_nonempty_line_scope(all_rules, "docs", ["a"])


class TestSyncCountsFlagContract:
    """`--sync-counts` 的守備範圍與 Makefile 接線（#1407 第三輪 F4）。"""

    _SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "tools"
               / "dx" / "bump_docs.py")

    def _run(self, *args):
        return subprocess.run([sys.executable, str(self._SCRIPT), *args],
                              capture_output=True, text=True)

    def test_make_version_check_runs_sync_counts(self):
        """`--sync-counts --check` 必須是 release gate 的一部分。

        NO-SOURCE 與計數的 DEAD / MISSING 三種診斷，原本在 repo 內只有
        .github/workflows/validate.yaml 的 path-filtered PR job 跑得到——
        `make pre-tag` 看不到，等於擋不住 tag。
        """
        makefile = (bump_docs.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        body = makefile.split("\nversion-check:", 1)
        assert len(body) == 2, "Makefile 沒有 version-check target"
        recipe = body[1].split("\n.PHONY", 1)[0]
        assert "--sync-counts --check" in recipe, (
            f"version-check 沒有跑 `--sync-counts --check`：\n{recipe}")
        assert "bump_docs.py --check" in recipe, recipe

    def test_pre_tag_depends_on_version_check(self):
        makefile = (bump_docs.REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        pre_tag = [ln for ln in makefile.splitlines()
                   if ln.startswith("pre-tag:")]
        assert pre_tag and "version-check" in pre_tag[0], pre_tag

    def test_sync_counts_is_green_today(self):
        r = self._run("--sync-counts", "--check")
        assert r.returncode == 0, r.stdout[-1200:]

    # ⛔ EVERY flag the guard claims to reject. The bug it exists to prevent
    # was itself a list that named five of six version lines and dropped
    # `tenant_api`; pinning a subset reproduces that shape one level up.
    @pytest.mark.parametrize("extra", [
        ["--platform", "2.10.0"], ["--exporter", "2.10.0"],
        ["--tools", "2.10.0"], ["--portal", "2.10.0"],
        ["--recipe-preview", "2.10.0"], ["--tenant-api", "2.10.0"],
        ["--scope", "docs"],
    ])
    def test_sync_counts_rejects_flags_it_would_discard(self, extra):
        """`--sync-counts` 不讀版號旗標也不讀 --scope，過去照收不誤且靜默丟棄。

        `--sync-counts --platform 2.10.0` 於是同步了計數、**沒有** bump 版號，
        然後 exit 0——release script 可以就這樣掉一整條版號線。
        """
        r = self._run("--sync-counts", "--check", *extra)
        assert r.returncode == 2, (r.returncode, r.stdout[-600:])
        assert "does not accept" in r.stderr, r.stderr[-400:]


class TestShieldsBadgeEscaping:
    """README 版號 badge 的 shields.io 欄位跳脫（#1407 第三輪 F5）。

    static badge 的路徑文法是 `/badge/<label>-<message>-<color>`：三個欄位用
    單一 `-` 分隔，欄位**內文**的 `-` 必須寫成 `--`。正式版號沒有 `-`，
    release candidate 有——`_SEMVER` 放寬到接受 pre-release 之後，
    `--platform 2.10.0-rc1` 就會吐出 `badge/version-v2.10.0-rc1-brightgreen`
    （四個欄位），整個 rc 視窗都渲染錯誤。

    ⛔ 這個 bug 不會被任何既有 gate 抓到：pattern 咬得到自己的輸出（冪等測試
    綠）、值 round-trip 得回來、`--check` 全程報 consistent。唯一症狀是
    README 上一張壞掉的圖。
    """

    def _badge_rules(self):
        rules = [r for r in bump_docs._build_rules()["platform"]
                 if r.get("desc", "").endswith("version badge")]
        assert len(rules) == 2, f"預期 README.md / README.en.md 兩條：{rules}"
        return rules

    def test_release_version_unchanged(self):
        """沒有 suffix 的版號不受影響——跳脫只能動到該動的字元。"""
        for rule in self._badge_rules():
            assert rule["replacement"]("2.10.0") == \
                "badge/version-v2.10.0-brightgreen"

    def test_prerelease_dash_is_doubled(self):
        for rule in self._badge_rules():
            assert rule["replacement"]("2.10.0-rc1") == \
                "badge/version-v2.10.0--rc1-brightgreen"

    def test_output_always_has_exactly_three_badge_fields(self):
        """把輸出照 shields.io 的規則解析回來：先把 `--` 佔位，再切 `-`。"""
        for rule in self._badge_rules():
            for ver in ("2.10.0", "2.10.0-rc1", "2.10.0-beta.2",
                        "3.0.0-rc1-hotfix"):
                out = rule["replacement"](ver)
                path = out[len("badge/"):]
                fields = path.replace("--", "\x00").split("-")
                assert len(fields) == 3, (
                    f"{ver!r} → {out!r} 解析成 {len(fields)} 個欄位 "
                    f"{[f.replace(chr(0), '-') for f in fields]}，"
                    f"shields.io 只認 label-message-color 三個")
                label, message, color = (f.replace("\x00", "-") for f in fields)
                assert (label, message, color) == ("version", f"v{ver}",
                                                   "brightgreen")

    def test_pattern_still_matches_the_escaped_form(self):
        """跳脫後的字串仍要被自己的 pattern 咬回來，否則下一次 --check 永遠 drift。"""
        for rule in self._badge_rules():
            out = rule["replacement"]("2.10.0-rc1")
            assert re.findall(rule["pattern"], out) == [out], out

    def test_escaping_is_confined_to_the_badge_rule(self):
        """其他規則不得被順手加上跳脫——`--` 在版號字串裡是壞掉的。"""
        for line, rules in bump_docs._build_rules().items():
            for rule in rules:
                if rule.get("desc", "").endswith("version badge"):
                    continue
                if rule.get("whole_file") or rule.get("pair_anchor"):
                    continue
                assert "--rc1" not in rule["replacement"]("9.9.9-rc1"), (
                    f"[{line}] {rule['desc']} 不該跳脫 dash")

    def test_real_readmes_are_parseable_today(self):
        for name in ("README.md", "README.en.md"):
            text = (bump_docs.REPO_ROOT / name).read_text(encoding="utf-8")
            found = re.findall(r"badge/version-(\S+?)-brightgreen", text)
            assert found, f"{name} 的版號 badge 不見了——規則會 DEAD"
            for message in found:
                assert "-" not in message.replace("--", ""), (
                    f"{name} badge 的 message 欄位含未跳脫的 dash：{message!r}")


class TestEveryDiagnosisIsWiredIntoEveryGatingMode:
    """⛔ 接線層鎖，補齊到「每一種診斷 × 每一個會擋 release 的模式」。

    這個分支的論點寫在 `TestGlobGroupHealth` 裡：`make pre-tag` 不跑 pytest，
    所以只有測試看得到的守門員，對 release gate 而言等於不存在。照那個論點，
    「只有 helper 的單元測試釘住」也一樣不存在——真正要釘的是 `main()` 有沒有
    把診斷接到結束碼上。

    盲審實測：`--check` 對 MISSING / DEAD / GLOB-EMPTY 的結束碼判斷可以整段
    刪掉、`--what-if` 的三個計數可以從結束碼拿掉、`--sync-counts` 的 plain 與
    check 分支、以及 explicit bump 的 `sys.exit(EXIT_VIOLATION)` 全都可以刪掉
    ——而整份測試維持全綠。以下每一條各對應一個當時存活的變異。

    ⚠️ 全部在 `tmp_path` 上跑（`REPO_ROOT` 被 monkeypatch），所以不會像某次
    mutation 那樣真的寫進 repo。
    """

    _MISSING = {
        "file": "docs/gone.md", "desc": "probe missing",
        "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"v{v}",
    }
    _DEAD = {
        "file": "docs/there.md", "desc": "probe dead",
        "pattern": r"\*\*絕不出現\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**絕不出現**：v{v}",
    }
    _GLOB_EMPTY = {
        "file": "__glob__", "glob_dir": "nowhere", "glob_pattern": "**/*.md",
        "desc": "probe glob-empty",
        "pattern": r"v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"v{v}",
    }
    # ⛔ GLOB-DEAD 原本不在這個軸上，而 class docstring 寫的是「每一種診斷 ×
    # 每一個會擋 release 的模式」——少一種診斷，這個 class 的名字就是假的。
    # 三個變異因此存活：`--what-if` 結束碼把 glob_dead 拿掉、`--what-if` 的
    # group verdict 迴圈整段閹掉、explicit bump 的 glob_broken 計數縮成
    # `("GLOB-EMPTY",)`。GLOB-DEAD 只有 `--check` 那條腿被
    # `TestGlobGroupHealth.test_check_cli_fails_on_a_dead_glob` 釘住。
    #
    # 展開到 1 個檔案（`docs/there.md` 由 `_repo` 寫出）、整棵樹 0 命中 —— 這
    # 是 GLOB-EMPTY 看不到、per-file require_match 也看不到的那一格。
    _GLOB_DEAD = {
        "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
        "desc": "probe glob-dead",
        "pattern": r"\*\*絕不出現\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**絕不出現**：v{v}",
    }

    def _repo(self, tmp_path, monkeypatch, rule):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "there.md").write_text("沒有版號\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "read_current_versions",
                            lambda: {line: "2.9.0"
                                     for line in bump_docs.VERSION_LINES})
        monkeypatch.setattr(bump_docs, "_build_rules", lambda: {
            line: ([dict(rule)] if line == "platform" else [])
            for line in bump_docs.VERSION_LINES})

    _PROBES = {"missing": _MISSING, "dead": _DEAD,
               "glob_empty": _GLOB_EMPTY, "glob_dead": _GLOB_DEAD}

    def test_the_diagnosis_axis_covers_every_glob_level_diagnosis(self):
        """⛔ 反空洞：軸縮回去（例如把 glob_dead 拿掉）必須是紅的，不是靜靜
        少跑幾個 case。`_PROBES` 是 parametrize 的唯一來源，這條釘住它的內容。
        """
        assert set(self._PROBES) == {
            "missing", "dead", "glob_empty", "glob_dead"}, sorted(self._PROBES)

    @pytest.mark.parametrize("diagnosis", sorted(_PROBES))
    @pytest.mark.parametrize("mode", ["--check", "--what-if"])
    def test_gating_modes_exit_nonzero(self, diagnosis, mode, tmp_path,
                                       monkeypatch, capsys, cli_argv):
        rule = self._PROBES[diagnosis]
        self._repo(tmp_path, monkeypatch, rule)
        cli_argv("bump_docs", mode)
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0, (
            f"{mode} 對 {diagnosis} 仍 exit 0——release gate 會說版號全部一致，"
            "而那條規則其實一個字都沒驅動。")
        # ⛔ 結束碼非 0 還不夠：要確定紅的是**這一種**診斷。`--what-if` 的
        # group verdict 迴圈被閹掉時，glob_dead 那格會因為別的計數而僥倖非 0
        # ——標籤才分得出「有接住」與「剛好也不是 0」。
        label = {"missing": "MISSING", "dead": "DEAD",
                 "glob_empty": "GLOB-EMPTY", "glob_dead": "GLOB-DEAD"}[diagnosis]
        assert label in out, (
            f"{mode} 沒有印出 {label}——結束碼非 0 可能來自別的原因，這個診斷"
            f"本身仍是隱形的。\n{out}")

    @pytest.mark.parametrize("diagnosis", sorted(_PROBES))
    def test_explicit_bump_exits_nonzero(self, diagnosis, tmp_path,
                                         monkeypatch, capsys, cli_argv):
        """`make bump-docs` 走的就是這條路徑——真正的 release bump。"""
        rule = self._PROBES[diagnosis]
        self._repo(tmp_path, monkeypatch, rule)
        cli_argv("bump_docs", "--platform", "2.10.0")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        out = capsys.readouterr().out
        assert exc.value.code != 0, (
            f"explicit bump 對 {diagnosis} 仍 exit 0——版號被推上去了，而這條"
            "規則沒有跟上，沒有任何人會知道。")
        if diagnosis in ("glob_empty", "glob_dead"):
            # glob_broken 的計數縮成 `("GLOB-EMPTY",)` 時 GLOB-DEAD 會落回
            # 「什麼都沒計」——結束碼還是可能非 0，但那段收尾訊息不會印。
            assert "glob rule(s) are GLOB-EMPTY" in out, (
                f"explicit bump 沒有把 {diagnosis} 計進 glob_broken——"
                f"那棵樹在一次 release bump 裡整個沒被碰到，而收尾沒說。\n{out}")


class TestRoundFourSurvivors:
    """變異存活者的補釘（第四輪 lens M / O 交叉確認）。"""

    def test_plain_sync_counts_writing_mode_fails_on_a_broken_count(
            self, tmp_path, monkeypatch, capsys, cli_argv):
        """⛔ `--sync-counts` 三個分支裡，唯一**會寫檔**的那個沒有保護。

        原本聲稱釘住它的測試傳的是 `--dry-run`，所以走的是另一條分支——
        名字與 docstring 描述的正是沒被涵蓋的那一個。人手跑
        `--sync-counts` 修數字時，若某條 count 規則是 NO-SOURCE / DEAD /
        MISSING 而結束碼判斷不見了，畫面上只會剩 `✅ Done.`。
        """
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "_build_count_rules", lambda: [{
            "file": "docs/gone.md", "desc": "probe count",
            "pattern": r"[0-9]+ tools", "count": 3,
            "replacement": lambda n: f"{n} tools",
            "source_ok": True, "source": "probe",
        }])
        cli_argv("bump_docs", "--sync-counts")
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        capsys.readouterr()
        assert exc.value.code != 0, (
            "plain（會寫檔的）--sync-counts 對壞掉的 count 規則仍 exit 0")

    def test_a_glob_rule_cannot_smuggle_a_rule_over_the_appversion_pins(self):
        """⛔ 反向釘只看了 `"file"` 這個 key，而 glob 規則的 `"file"` 是字面
        字串 `"__glob__"`——所以一條掃過那三個 appVersion pin 的 glob 完全
        繞過它。要比對的是**展開後**的檔案集合。
        """
        protected = {
            "k8s/04-tenant-api/deployment.yaml",
            "docs/assets/platform-data.json",
            "tools/portal/src/interactive/tools/_common/data/images.js",
        }
        expanded = {
            r.get("file")
            for r in bump_docs._expand_glob_rules(
                bump_docs._build_rules()["tenant-api"])
        }
        assert expanded.isdisjoint(protected), (
            "tenant-api 線有規則會改寫刻意追 appVersion 的 image pin："
            f"{sorted(expanded & protected)}")

    def test_the_portal_jsx_globs_are_actually_targeted(self):
        """⛔ #1407 的招牌修法（JSX front matter / image pin 兩組 glob）可以被
        整組刪掉而測試全綠：沒有任何斷言說「有規則指向 PORTAL_JSX_DIR」。
        既有的測試只檢查那個目錄裡有 .jsx，反空洞下限又寬到刪六條都看不見。
        """
        rooted = [
            r for rules in bump_docs._build_rules().values() for r in rules
            if r.get("file") == "__glob__"
            and r.get("glob_dir") == bump_docs.PORTAL_JSX_DIR
        ]
        assert len(rooted) >= 3, (
            "portal 那棵樹上的 glob 規則消失了——44 份 JSX front matter 停在 "
            f"舊版號正是 #1407 的成因。目前 rooted={len(rooted)}")

    def test_the_docs_globs_are_actually_targeted(self):
        """⛔ 上一條替 `tools/portal/` 關上的那個門，`docs/` 這邊還開著。

        實測：把 `doc header blockquote version (> **vX.Y.Z |)` 整條刪掉——
        它今天驅動 **26 個真的檔案**——整份測試維持全綠。既有的下限都太鬆：
        `test_every_glob_rule_expands_to_at_least_one_file` 是 `>= 5` 對 11、
        `test_the_invariant_has_teeth` 是 `>= 6` 對 8，刪一條都碰不到。
        `test_the_portal_jsx_globs_are_actually_targeted` 夠緊，但只守
        PORTAL_JSX_DIR。

        兩段斷言各擋一半：
          1. 數量下限（緊貼今天的 8 條）——刪掉任何一條 docs glob 就紅。
          2. 那條 blockquote 規則**照形狀**指認（不是照 desc 字串），並要求
             它今天仍然驅動一批真的檔案。加一條無關的 docs glob 來湊數字，
             過不了這一關。
        """
        rooted = [
            r for rules in bump_docs._build_rules().values() for r in rules
            if r.get("file") == "__glob__" and r.get("glob_dir") == "docs"
        ]
        assert len(rooted) >= 8, (
            "docs/ 樹上的 glob 規則少了——每一條都橫跨 260 個檔案，刪掉一條"
            "不會讓任何既有下限紅，但那批檔案的版號會就地凍結（#1407 的失敗"
            f"模式，只是換一棵樹）。目前 rooted={len(rooted)}")

        # ⛔ 照形狀指認：`> **vX.Y.Z** | …` 這個 doc header blockquote。
        # 用 desc 字串當 key 的話，改一次措辭這條就變成假綠。
        probe = "> **v2.9.0** | 2026-06-06"
        blockquote = [r for r in rooted if re.search(r["pattern"], probe)]
        assert len(blockquote) == 1, (
            "找不到（或找到不只一條）驅動 `> **vX.Y.Z** |` doc header 的 "
            f"docs glob。目前 {[r['desc'] for r in blockquote]}")

        driven = [
            e["file"] for e in bump_docs._expand_glob_rules([dict(blockquote[0])])
            if re.search(blockquote[0]["pattern"],
                         (bump_docs.REPO_ROOT / e["file"]).read_text(
                             encoding="utf-8"), re.MULTILINE)
        ]
        assert len(driven) >= 20, (
            f"`{blockquote[0]['desc']}` 今天只驅動 {len(driven)} 個檔案"
            "（歷史值 26）——這條 glob 正在死掉或已經被改窄，而 GLOB-DEAD 只在"
            "「一個都沒撈到」時才會出聲。")


class TestSemverShapeGuard:
    """⛔ `_require_semver_shape()` 完全沒有測試——而它是上一個 commit 的招牌修法。

    實測的損害（原始碼 docstring 記的就是這個）：每一條規則都是**比對舊值**再
    改寫，所以第一次用壞版號跑一定成功——`--platform 2.10.0+build.5`（`+` 不在
    `_SEMVER` 的 suffix 字元類裡）與截斷 typo `--platform 2.10` 都印
    `✅ Done. 339 update(s) applied.` 並 exit 0。之後 `--check` 才永久轉紅，而
    工程師的第一個反應是把同一行再跑一次——於是後綴被接上第二次：
    `v2.10.0+build.5+build.5`。

    變異 `bad = []` 存活於整份測試套件。

    ⚠️ 全程 `REPO_ROOT` 被 monkeypatch 到 `tmp_path`，所以「寫了沒有」是在
    tmp 目錄上量的，不會像某一輪那樣真的寫進 repo 的 237 個檔案。
    """

    _RULE = {
        "file": "docs/x.md", "desc": "probe footer",
        "pattern": r"\*\*最後更新\*\*：v[0-9]+\.[0-9]+\.[0-9]+"
                   r"(?:-[a-zA-Z0-9._-]+)?",
        "replacement": lambda v: f"**最後更新**：v{v}",
    }
    _ORIGINAL = "# X\n\n**最後更新**：v2.9.0\n"

    def _repo(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        target = tmp_path / "docs" / "x.md"
        target.write_text(self._ORIGINAL, encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(bump_docs, "read_current_versions",
                            lambda: {line: "2.9.0"
                                     for line in bump_docs.VERSION_LINES})
        monkeypatch.setattr(bump_docs, "_build_rules", lambda: {
            line: ([dict(self._RULE)] if line == "platform" else [])
            for line in bump_docs.VERSION_LINES})
        return target

    # 六種壞形狀，各自代表一類真的會被打出來的東西：
    #   build metadata / 截斷 typo（兩種寫法）/ 四段 / 帶 build 的 prerelease /
    #   完全不是版號。
    @pytest.mark.parametrize("bad", [
        "2.10.0+build.5",
        "2.10",
        "v2.10",
        "2.10.0.1",
        "2.10.0-rc1+build.5",
        "latest",
    ])
    def test_a_malformed_version_exits_nonzero_and_writes_nothing(
            self, bad, tmp_path, monkeypatch, capsys, cli_argv):
        target = self._repo(tmp_path, monkeypatch)
        cli_argv("bump_docs", "--platform", bad)
        with pytest.raises(SystemExit) as exc:
            bump_docs.main()
        captured = capsys.readouterr()
        assert exc.value.code == bump_docs.EXIT_CALLER_ERROR, (
            f"--platform {bad!r} 沒有被擋下（exit={exc.value.code}）。這是"
            "caller error(2)：repo 沒有錯，是這個值寫不回去也比對不回來。")
        assert target.read_text(encoding="utf-8") == self._ORIGINAL, (
            f"--platform {bad!r} 已經改了檔案。損害必然先於偵測——第一次跑一定"
            "成功（規則比對的是舊值），紅燈要到下一次 --check 才出現，而那時"
            "後綴已經被接上去了。")
        assert "not a version this tool can" in captured.err, captured.err
        assert "Nothing was written" in captured.err, captured.err

    @pytest.mark.parametrize("good", [
        "2.10.0", "2.10.0-rc1", "2.10.0-preview.1", "v2.10.0",
    ])
    def test_a_well_formed_version_still_goes_through(
            self, good, tmp_path, monkeypatch, capsys, cli_argv):
        """反面：守門員不能順手把合法的 prerelease / `v` 前綴一起擋掉。"""
        target = self._repo(tmp_path, monkeypatch)
        cli_argv("bump_docs", "--platform", good)
        bump_docs.main()          # 不應 SystemExit
        out = capsys.readouterr().out
        assert f"v{good.lstrip('v')}" in target.read_text(encoding="utf-8"), (
            f"--platform {good!r} 是合法形狀卻沒有被寫進去。")
        assert "1 update(s) applied" in out, out

    def test_the_guard_is_the_same_shape_every_rule_pattern_is_built_from(self):
        """⛔ 驗收標準必須是 `_SEMVER` 本人，不是另抄一份正則。

        另抄一份的話，兩邊哪天飄開，被放行的值就會是「寫得進去、比對不回來」
        ——正是這個守門員存在的理由。
        """
        for good in ("2.10.0", "2.10.0-rc1", "2.10.0-preview.1"):
            assert re.fullmatch(bump_docs._SEMVER, good), good
            bump_docs._require_semver_shape([("platform", good)])
        for bad in ("2.10.0+build.5", "2.10"):
            assert not re.fullmatch(bump_docs._SEMVER, bad), bad
            with pytest.raises(SystemExit):
                bump_docs._require_semver_shape([("platform", bad)])


class TestScopedRulesNarrowsAfterExpansion:
    """⛔ `_scoped_rules()` 的**展開後**過濾沒有任何測試（`grep -rn _scoped_rules
    tests/` 為 0 命中）。

    兩道過濾是刻意的：展開**前**那道要寬鬆，否則掛在 scope 上方的 `docs/**`
    glob 會整條被丟掉（那條 glob 治理的正是被 scope 的檔案）；展開**後**那道
    要嚴格，否則同一份寬鬆把規則套到整棵樹上，一次被 scope 的 bump 悄悄變成
    全 repo 的 bump。

    實測：把後者改成 `if True:` 之後
    `--platform 9.9.9 --scope docs/integration --dry-run` 從 42 個檔變成 278 個。
    """

    _GLOB = {
        "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
        "desc": "probe footer",
        "pattern": r"\*\*最後更新\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**最後更新**：v{v}",
    }

    def _tree(self, tmp_path, monkeypatch):
        for rel in ("docs/integration", "docs/other"):
            (tmp_path / rel).mkdir(parents=True)
        for rel in ("docs/top.md", "docs/integration/a.md", "docs/other/b.md"):
            (tmp_path / rel).write_text("**最後更新**：v2.9.0\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

    def test_expansion_outside_the_scope_is_dropped(self, tmp_path, monkeypatch):
        self._tree(tmp_path, monkeypatch)
        files = {r["file"] for r in
                 bump_docs._scoped_rules([dict(self._GLOB)], "docs/integration")}
        assert files == {"docs/integration/a.md"}, (
            f"被 scope 的 bump 撈到了 scope 外的檔案：{sorted(files)}。展開前"
            "那道過濾故意寬鬆（要留住掛在上方的 glob），所以展開後這道就是"
            "唯一擋住「悄悄變成全 repo bump」的東西。")

    def test_the_pre_expansion_filter_is_still_generous(self, tmp_path,
                                                        monkeypatch):
        """反面：不能靠把展開前那道一起收緊來「解決」——那會把 glob 整條丟掉。"""
        self._tree(tmp_path, monkeypatch)
        assert bump_docs._filter_by_scope([dict(self._GLOB)],
                                          "docs/integration"), (
            "掛在 scope 上方的 docs/** glob 在展開前就被丟掉了——被 scope 的"
            "那些檔案於是完全沒有規則治理。")

    def test_sentinels_survive_the_post_expansion_filter(self, tmp_path,
                                                         monkeypatch):
        """塌成 0 個檔案的 glob 帶的是診斷不是檔案，被 scope 濾掉就恢復沉默。"""
        (tmp_path / "docs").mkdir()
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        out = bump_docs._scoped_rules([dict(self._GLOB)], "docs/integration")
        assert [r for r in out if r.get("glob_collapsed")], out

    def test_cli_scoped_dry_run_stays_inside_the_scope(self):
        """接線層鎖：走真的 repo、真的 CLI，只用 `--dry-run`（不寫檔）。"""
        script = (Path(__file__).resolve().parents[2] / "scripts" / "tools"
                  / "dx" / "bump_docs.py")
        r = subprocess.run(
            [sys.executable, str(script), "--platform", "9.9.9",
             "--scope", "docs/integration", "--dry-run"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-800:]
        stray = sorted({m for m in re.findall(r"docs/[\w./-]+\.md", r.stdout)
                        if not m.startswith("docs/integration/")})
        assert not stray, (
            "被 scope 到 docs/integration 的 dry-run 報告了 scope 外的檔案："
            f"{stray[:10]}（共 {len(stray)}）——展開後那道過濾沒有作用，一次"
            "局部 bump 會變成全 repo bump。")


class TestScopeNarrowedGlobHealth:
    """⛔ `scope_narrowed` 兩個方向都沒有測試（兩個變異都存活）。

    GLOB-DEAD 是**整條 glob** 的性質，不是「你 scope 到的那一小塊」的性質。
    `--scope docs/integration` 底下，一條 `docs/**` 的規則在那個子集裡撈不到
    是完全正常的（它在整棵樹上很健康），把它報成 dead 會讓一次合法的局部
    bump 因為一個不存在的缺陷而失敗——實測 `--check --scope docs/integration`
    從 rc 0 變 rc 1。

    兩個變異各對應下面一條：
      1. `_scoped_rules()` 不再蓋 `scope_narrowed=True`。
      2. `_note_glob()` 不再 `if rule.get("scope_narrowed"): return`。
    """

    _GLOB = {
        "file": "__glob__", "glob_dir": "docs", "glob_pattern": "**/*.md",
        "desc": "probe footer",
        "pattern": r"\*\*最後更新\*\*：v[0-9]+\.[0-9]+\.[0-9]+",
        "replacement": lambda v: f"**最後更新**：v{v}",
    }

    def _tree(self, tmp_path, monkeypatch):
        for rel in ("docs/integration", "docs/other"):
            (tmp_path / rel).mkdir(parents=True)
        # scope 內那個檔案沒有頁尾、scope 外那個有——整條 glob 是活的，
        # 但被 scope 的子集是 0 命中。
        (tmp_path / "docs/integration/a.md").write_text(
            "# A\n沒有頁尾\n", encoding="utf-8")
        (tmp_path / "docs/other/b.md").write_text(
            "**最後更新**：v2.9.0\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)

    def test_scoped_expansion_carries_the_marker(self, tmp_path, monkeypatch):
        self._tree(tmp_path, monkeypatch)
        scoped = bump_docs._scoped_rules([dict(self._GLOB)], "docs/integration")
        assert scoped, "scope 內沒有選到任何規則，下面的斷言會是空跑"
        assert all(r.get("scope_narrowed") for r in scoped), (
            "被 scope narrow 過的 glob 展開沒有蓋上 scope_narrowed——"
            "`_note_glob()` 因此無從得知這只是整條 glob 的一小塊。")

    def test_an_unscoped_expansion_is_not_marked(self, tmp_path, monkeypatch):
        """反面：無 scope 時不能蓋，否則 GLOB-DEAD 對所有 glob 永久失效。"""
        self._tree(tmp_path, monkeypatch)
        unscoped = bump_docs._scoped_rules([dict(self._GLOB)], None)
        assert not any(r.get("scope_narrowed") for r in unscoped), unscoped

    def test_a_scoped_run_does_not_invent_a_glob_dead(self, tmp_path,
                                                      monkeypatch):
        self._tree(tmp_path, monkeypatch)
        scoped = bump_docs._scoped_rules([dict(self._GLOB)], "docs/integration")
        statuses = [c[0] for c in
                    bump_docs.apply_rules(scoped, "2.9.0", check_only=True)]
        assert "GLOB-DEAD" not in statuses, (
            "被 scope 的子集裡撈不到就被報成 GLOB-DEAD——那條 glob 在整棵樹上"
            f"是活的，這是一個不存在的缺陷擋掉合法的局部 bump：{statuses}")

    def test_the_same_glob_is_still_dead_when_run_unscoped(self, tmp_path,
                                                           monkeypatch):
        """⛔ 反面鎖：豁免只限被 scope 的那條路徑。

        沒有這條的話，「一律不報 GLOB-DEAD」也能讓上一條變綠——而那正是
        #1407 花整輪拔掉的沉默。
        """
        (tmp_path / "docs" / "integration").mkdir(parents=True)
        (tmp_path / "docs/integration/a.md").write_text(
            "# A\n沒有頁尾\n", encoding="utf-8")
        monkeypatch.setattr(bump_docs, "REPO_ROOT", tmp_path)
        statuses = [c[0] for c in bump_docs.apply_rules(
            bump_docs._scoped_rules([dict(self._GLOB)], None), "2.9.0",
            check_only=True)]
        assert "GLOB-DEAD" in statuses, statuses

    def test_cli_scoped_check_is_green_on_the_real_repo(self):
        """接線層鎖：`--check --scope docs/integration` 必須是 rc 0。

        少了 `scope_narrowed` 這一整套，這條命令會因為一批「在整棵 docs/ 樹上
        很健康」的 glob 而 exit 1。
        """
        script = (Path(__file__).resolve().parents[2] / "scripts" / "tools"
                  / "dx" / "bump_docs.py")
        r = subprocess.run(
            [sys.executable, str(script), "--check", "--scope",
             "docs/integration"], capture_output=True, text=True)
        assert r.returncode == 0, (
            "被 scope 的 --check 因為 GLOB-DEAD 之類的 glob 診斷而紅了——"
            f"那些診斷是整條 glob 的性質，不該由子集判定。\n{r.stdout[-1500:]}")
        assert "GLOB-DEAD" not in r.stdout, r.stdout[-1500:]
