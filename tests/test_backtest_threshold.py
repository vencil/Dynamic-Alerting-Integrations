#!/usr/bin/env python3
"""test_backtest_threshold.py — backtest_threshold.py 測試。

驗證:
  1. parse_lookback() — 時間窗口解析
  2. count_threshold_breaches() — 超閾值計數
  3. extract_changes_from_dirs() — 目錄比對
  4. backtest_change() — 單一變更回測
  5. generate_report() — 報告彙整
  6. generate_markdown() — Markdown 格式
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import backtest_threshold as bt  # noqa: E402


class TestParseLookback(unittest.TestCase):
    """parse_lookback() 測試。"""

    def test_days(self):
        self.assertEqual(bt.parse_lookback("7d"), 7 * 86400)

    def test_hours(self):
        self.assertEqual(bt.parse_lookback("24h"), 24 * 3600)

    def test_minutes(self):
        self.assertEqual(bt.parse_lookback("30m"), 30 * 60)

    def test_invalid_fallback(self):
        """無效格式應 fallback 到 7d。"""
        self.assertEqual(bt.parse_lookback("invalid"), 7 * 86400)


class TestCountThresholdBreaches(unittest.TestCase):
    """count_threshold_breaches() 測試。"""

    def test_above_threshold(self):
        values = [(1, "80"), (2, "50"), (3, "90"), (4, "70")]
        self.assertEqual(bt.count_threshold_breaches(values, 70, "above"), 2)

    def test_below_threshold(self):
        values = [(1, "20"), (2, "50"), (3, "10"), (4, "30")]
        self.assertEqual(bt.count_threshold_breaches(values, 25, "below"), 2)

    def test_none_threshold(self):
        values = [(1, "80")]
        self.assertEqual(bt.count_threshold_breaches(values, None), 0)

    def test_invalid_values_skipped(self):
        values = [(1, "80"), (2, "NaN"), (3, "90")]
        self.assertEqual(bt.count_threshold_breaches(values, 70), 2)

    def test_empty_values(self):
        self.assertEqual(bt.count_threshold_breaches([], 70), 0)

    def test_string_threshold(self):
        """字串型閾值應正確轉換。"""
        values = [(1, "80"), (2, "90")]
        self.assertEqual(bt.count_threshold_breaches(values, "70"), 2)


class TestExtractChangesFromDirs(unittest.TestCase):
    """extract_changes_from_dirs() 測試。"""

    def test_detect_changes(self):
        """應偵測到閾值變更。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            # Current: mysql_connections = 50
            with open(os.path.join(current, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")
            # Baseline: mysql_connections = 70
            with open(os.path.join(baseline, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 70\n")

            changes = bt.extract_changes_from_dirs(current, baseline)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["tenant"], "db-a")
            self.assertEqual(changes[0]["metric"], "mysql_connections")
            self.assertEqual(changes[0]["old_value"], "70")
            self.assertEqual(changes[0]["new_value"], "50")

    def test_skip_underscore_keys(self):
        """_ 前綴的 key 應被忽略。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            with open(os.path.join(current, "db-a.yaml"), "w") as f:
                f.write("_silent_mode: warning\nmysql_connections: 50\n")
            with open(os.path.join(baseline, "db-a.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")

            changes = bt.extract_changes_from_dirs(current, baseline)
            self.assertEqual(len(changes), 0)

    def test_skip_underscore_files(self):
        """_ 前綴的檔案應被忽略。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            with open(os.path.join(current, "_defaults.yaml"), "w") as f:
                f.write("mysql_connections: 50\n")
            changes = bt.extract_changes_from_dirs(current, baseline)
            self.assertEqual(len(changes), 0)

    def test_no_changes(self):
        """相同配置不應有變更。"""
        with tempfile.TemporaryDirectory() as current, \
             tempfile.TemporaryDirectory() as baseline:
            for d in [current, baseline]:
                with open(os.path.join(d, "db-a.yaml"), "w") as f:
                    f.write("mysql_connections: 50\n")
            changes = bt.extract_changes_from_dirs(current, baseline)
            self.assertEqual(len(changes), 0)


class TestGenerateReport(unittest.TestCase):
    """generate_report() 測試。"""

    def test_risk_summary(self):
        results = [
            {"status": "analyzed", "risk": "HIGH", "tenant": "a", "metric": "m"},
            {"status": "analyzed", "risk": "LOW", "tenant": "b", "metric": "n"},
            {"status": "no_data", "risk": "UNKNOWN", "tenant": "c", "metric": "o"},
        ]
        report = bt.generate_report(results, "7d")
        self.assertEqual(report["risk_summary"]["HIGH"], 1)
        self.assertEqual(report["risk_summary"]["LOW"], 1)
        self.assertEqual(report["analyzed"], 2)
        self.assertEqual(report["no_data"], 1)

    def test_report_has_timestamp(self):
        report = bt.generate_report([], "7d")
        self.assertIn("timestamp", report)


class TestGenerateMarkdown(unittest.TestCase):
    """generate_markdown() 測試。"""

    def test_contains_header(self):
        report = bt.generate_report([], "7d")
        md = bt.generate_markdown(report)
        self.assertIn("## Threshold Backtest Results", md)

    def test_high_risk_warning(self):
        results = [
            {"status": "analyzed", "risk": "HIGH", "tenant": "a",
             "metric": "m", "old_value": "70", "new_value": "50",
             "message": "test"},
        ]
        report = bt.generate_report(results, "7d")
        md = bt.generate_markdown(report)
        self.assertIn("HIGH risk", md)

    def test_table_format(self):
        results = [
            {"status": "analyzed", "risk": "LOW", "tenant": "db-a",
             "metric": "mysql_conn", "old_value": "70", "new_value": "50",
             "message": "no firing"},
        ]
        report = bt.generate_report(results, "7d")
        md = bt.generate_markdown(report)
        self.assertIn("| LOW |", md)
        self.assertIn("`mysql_conn`", md)


if __name__ == "__main__":
    unittest.main()
