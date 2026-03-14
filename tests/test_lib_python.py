#!/usr/bin/env python3
"""test_lib_python.py — _lib_python.py 共用函式庫測試。

驗證:
  1. parse_duration_seconds() — Prometheus duration 解析
  2. format_duration() — seconds → duration 格式化
  3. is_disabled() — 三態 disable 判定
  4. load_yaml_file() — YAML 載入 + 錯誤處理
"""

import os
import stat
import tempfile
import unittest


import _lib_python as lib  # noqa: E402


class TestParseDurationSeconds(unittest.TestCase):
    """parse_duration_seconds() 測試。"""

    def test_seconds(self):
        self.assertEqual(lib.parse_duration_seconds("30s"), 30)

    def test_minutes(self):
        self.assertEqual(lib.parse_duration_seconds("5m"), 300)

    def test_hours(self):
        self.assertEqual(lib.parse_duration_seconds("4h"), 14400)

    def test_days(self):
        self.assertEqual(lib.parse_duration_seconds("1d"), 86400)

    def test_float_duration(self):
        self.assertEqual(lib.parse_duration_seconds("1.5h"), 5400)

    def test_int_passthrough(self):
        self.assertEqual(lib.parse_duration_seconds(60), 60)

    def test_float_passthrough(self):
        self.assertEqual(lib.parse_duration_seconds(3.14), 3)

    def test_invalid_returns_none(self):
        for bad in ("abc", "", None, "5x", "s", [], {}):
            self.assertIsNone(lib.parse_duration_seconds(bad), msg=f"input={bad!r}")


class TestFormatDuration(unittest.TestCase):
    """format_duration() 測試。"""

    def test_seconds(self):
        self.assertEqual(lib.format_duration(30), "30s")

    def test_minutes(self):
        self.assertEqual(lib.format_duration(300), "5m")

    def test_hours(self):
        self.assertEqual(lib.format_duration(3600), "1h")

    def test_no_day_suffix(self):
        self.assertEqual(lib.format_duration(86400), "24h")
        self.assertEqual(lib.format_duration(259200), "72h")


class TestIsDisabled(unittest.TestCase):
    """is_disabled() 三態判定測試。"""

    def test_disable_variants(self):
        for val in ("disable", "disabled", "off", "false",
                    "Disable", "DISABLED", " OFF ", "False"):
            self.assertTrue(lib.is_disabled(val), msg=f"input={val!r}")

    def test_non_disabled(self):
        for val in ("enable", "warning", "critical", "all", "true", "on"):
            self.assertFalse(lib.is_disabled(val), msg=f"input={val!r}")

    def test_empty_and_none(self):
        self.assertFalse(lib.is_disabled(""))
        self.assertFalse(lib.is_disabled(None))
        self.assertFalse(lib.is_disabled(42))


class TestLoadYamlFile(unittest.TestCase):
    """load_yaml_file() 測試。"""

    def test_load_valid_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("key: value\nlist:\n  - a\n  - b\n")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            result = lib.load_yaml_file(path)
            self.assertEqual(result, {"key": "value", "list": ["a", "b"]})

    def test_missing_file_returns_default(self):
        self.assertIsNone(lib.load_yaml_file("/nonexistent/file.yaml"))
        self.assertEqual(lib.load_yaml_file("/nonexistent/file.yaml", default={}), {})

    def test_empty_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "empty.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            self.assertIsNone(lib.load_yaml_file(path))
            self.assertEqual(lib.load_yaml_file(path, default=[]), [])

    def test_none_path(self):
        self.assertIsNone(lib.load_yaml_file(None))


if __name__ == "__main__":
    unittest.main()
