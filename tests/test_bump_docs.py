#!/usr/bin/env python3
"""test_bump_docs.py — 版號一致性管理工具 測試套件 (Phase 10)。

驗證 bump_docs.py 的核心功能:
  1. _build_rules() 規則結構完整性
  2. apply_rules() check-only 與寫入模式
  3. read_current_versions() 版號讀取
  4. Lambda replacement 正確性

用法:
  python3 -m pytest tests/test_bump_docs.py -v
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# Add scripts/tools to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import bump_docs  # noqa: E402


class TestBuildRules(unittest.TestCase):
    """測試 _build_rules() 規則結構。"""

    def test_returns_three_lines(self):
        rules = bump_docs._build_rules()
        self.assertIn("platform", rules)
        self.assertIn("exporter", rules)
        self.assertIn("tools", rules)

    def test_all_rules_have_required_keys(self):
        rules = bump_docs._build_rules()
        for line_name, line_rules in rules.items():
            for rule in line_rules:
                self.assertIn("file", rule, f"Missing 'file' in {line_name} rule")
                self.assertIn("desc", rule, f"Missing 'desc' in {line_name} rule")
                self.assertIn("replacement", rule, f"Missing 'replacement' in {line_name} rule")
                # Either 'pattern' or 'whole_file' must exist
                has_pattern = "pattern" in rule or "whole_file" in rule
                self.assertTrue(has_pattern, f"Missing pattern/whole_file in {line_name} rule")

    def test_tools_rules_reference_da_tools(self):
        """da-tools 規則應引用 da-tools 相關檔案。"""
        rules = bump_docs._build_rules()
        tool_files = [r["file"] for r in rules["tools"]]
        self.assertTrue(any("da-tools" in f for f in tool_files))

    def test_platform_rules_reference_chart(self):
        """Platform 規則應包含 Chart.yaml。"""
        rules = bump_docs._build_rules()
        plat_files = [r["file"] for r in rules["platform"]]
        self.assertTrue(any("Chart.yaml" in f for f in plat_files))


class TestApplyRulesCheckOnly(unittest.TestCase):
    """測試 apply_rules() check-only 模式。"""

    def test_check_detects_outdated(self):
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

            # Override REPO_ROOT temporarily
            orig_root = bump_docs.REPO_ROOT
            try:
                bump_docs.REPO_ROOT = Path(tmpdir)
                changes = bump_docs.apply_rules(rules, "0.2.0", check_only=True)
                statuses = [c[0] for c in changes]
                self.assertIn("UPDATE", statuses)

                # File should NOT be modified in check mode
                with open(test_file, 'r') as f:
                    content = f.read()
                self.assertIn("0.1.0", content)
            finally:
                bump_docs.REPO_ROOT = orig_root

    def test_check_passes_when_current(self):
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

            orig_root = bump_docs.REPO_ROOT
            try:
                bump_docs.REPO_ROOT = Path(tmpdir)
                changes = bump_docs.apply_rules(rules, "0.2.0", check_only=True)
                statuses = [c[0] for c in changes]
                self.assertIn("OK", statuses)
            finally:
                bump_docs.REPO_ROOT = orig_root


class TestApplyRulesWrite(unittest.TestCase):
    """測試 apply_rules() 寫入模式。"""

    def test_write_updates_file(self):
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

            orig_root = bump_docs.REPO_ROOT
            try:
                bump_docs.REPO_ROOT = Path(tmpdir)
                changes = bump_docs.apply_rules(rules, "0.3.0", check_only=False)
                statuses = [c[0] for c in changes]
                self.assertIn("UPDATE", statuses)

                # File should be modified
                with open(test_file, 'r') as f:
                    content = f.read()
                self.assertIn("0.3.0", content)
                self.assertNotIn("0.1.0", content)
            finally:
                bump_docs.REPO_ROOT = orig_root

    def test_whole_file_mode(self):
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

            orig_root = bump_docs.REPO_ROOT
            try:
                bump_docs.REPO_ROOT = Path(tmpdir)
                bump_docs.apply_rules(rules, "0.2.0", check_only=False)

                with open(test_file, 'r') as f:
                    content = f.read()
                self.assertEqual(content.strip(), "0.2.0")
            finally:
                bump_docs.REPO_ROOT = orig_root


class TestApplyRulesEdgeCases(unittest.TestCase):
    """邊界案例。"""

    def test_missing_file_returns_skip(self):
        """檔案不存在應回傳 SKIP。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rules = [{"file": "nonexistent.md", "desc": "missing",
                       "pattern": r"v\d+", "replacement": lambda v: f"v{v}"}]
            orig_root = bump_docs.REPO_ROOT
            try:
                bump_docs.REPO_ROOT = Path(tmpdir)
                changes = bump_docs.apply_rules(rules, "1.0.0", check_only=True)
                self.assertEqual(changes[0][0], "SKIP")
            finally:
                bump_docs.REPO_ROOT = orig_root

    def test_no_match_returns_ok(self):
        """Pattern 不匹配應回傳 OK。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.md")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("No version here\n")
            os.chmod(test_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

            rules = [{"file": "test.md", "desc": "test",
                       "pattern": r"v\d+\.\d+\.\d+",
                       "replacement": lambda v: f"v{v}"}]
            orig_root = bump_docs.REPO_ROOT
            try:
                bump_docs.REPO_ROOT = Path(tmpdir)
                changes = bump_docs.apply_rules(rules, "1.0.0", check_only=True)
                self.assertEqual(changes[0][0], "OK")
            finally:
                bump_docs.REPO_ROOT = orig_root


class TestReadCurrentVersions(unittest.TestCase):
    """測試版號讀取。"""

    def test_reads_from_real_repo(self):
        """從真實 repo 讀取版號 (若 Chart.yaml 存在)。"""
        versions = bump_docs.read_current_versions()
        if bump_docs.CHART_YAML.exists():
            self.assertIn("platform", versions)
            self.assertIn("exporter", versions)
        if bump_docs.DA_TOOLS_VERSION.exists():
            self.assertIn("tools", versions)


if __name__ == "__main__":
    unittest.main()
