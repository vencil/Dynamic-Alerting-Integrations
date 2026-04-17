#!/usr/bin/env python3
"""test_snapshot_v2.py — v2.1.0 新工具輸出格式快照測試。

驗證:
  1. alert_correlate build_report() 結構穩定性
  2. drift_detect build_summary() 結構穩定性
  3. check_bilingual_content format_json_report() 結構穩定性

快照更新: UPDATE_SNAPSHOTS=1 python -m pytest tests/test_snapshot_v2.py
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.snapshot

import alert_correlate as ac
import drift_detect as dd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "tools" / "lint"))
import check_bilingual_content as cbc  # noqa: E402

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "snapshots")


# ── Helpers ───────────────────────────────────────────────────

def _load_snapshot(name):
    """Load JSON snapshot from snapshots/ directory."""
    path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_snapshot(name, data):
    """Save JSON snapshot to snapshots/ directory."""
    path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)


def _assert_snapshot(name, data):
    """Assert data matches snapshot, or update if UPDATE_SNAPSHOTS=1."""
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        _save_snapshot(name, data)
        return
    expected = _load_snapshot(name)
    assert data == expected, (
        f"Snapshot mismatch for '{name}'. "
        f"Run UPDATE_SNAPSHOTS=1 to update."
    )


# ── Fixtures ──────────────────────────────────────────────────

def _make_sample_alerts():
    """Create sample AlertEvents for snapshot testing."""
    from datetime import datetime, timezone
    base = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    return [
        ac.AlertEvent(
            tenant="db-a", alertname="MariaDBHighConnections",
            severity="warning", namespace="db-a",
            starts_at=base.timestamp(),
            ends_at=(base.timestamp() + 300),
            labels={"alertname": "MariaDBHighConnections", "tenant": "db-a"},
        ),
        ac.AlertEvent(
            tenant="db-a", alertname="MariaDBHighCPU",
            severity="warning", namespace="db-a",
            starts_at=(base.timestamp() + 60),
            ends_at=(base.timestamp() + 600),
            labels={"alertname": "MariaDBHighCPU", "tenant": "db-a"},
        ),
        ac.AlertEvent(
            tenant="db-b", alertname="PostgreSQLHighConnections",
            severity="critical", namespace="db-b",
            starts_at=(base.timestamp() + 3600),
            ends_at=(base.timestamp() + 5400),
            labels={"alertname": "PostgreSQLHighConnections", "tenant": "db-b"},
        ),
    ]


# ── Alert Correlate Snapshots ─────────────────────────────────


class TestAlertCorrelateSnapshot:
    """alert_correlate 輸出結構快照。"""

    def test_report_structure(self):
        """build_report() 報告結構穩定。"""
        alerts = _make_sample_alerts()
        clusters = ac.analyze_alerts(alerts, window_secs=600, min_score=0.3)
        report = ac.build_report(clusters, len(alerts))

        # Normalize timestamp for snapshot stability
        report["timestamp"] = "2025-01-15T10:00:00+00:00"
        _assert_snapshot("alert_correlate_report", report)

    def test_empty_report(self):
        """空告警的報告結構。"""
        report = ac.build_report([], 0)
        report["timestamp"] = "2025-01-15T10:00:00+00:00"
        _assert_snapshot("alert_correlate_empty", report)


# ── Drift Detect Snapshots ────────────────────────────────────


class TestDriftDetectSnapshot:
    """drift_detect 輸出結構快照。"""

    def test_summary_structure(self, tmp_path):
        """build_summary() 摘要結構穩定。"""
        d1 = tmp_path / "cluster-a"
        d2 = tmp_path / "cluster-b"
        d1.mkdir()
        d2.mkdir()

        (d1 / "db-shared.yaml").write_text("shared: true", encoding="utf-8")
        (d2 / "db-shared.yaml").write_text("shared: true", encoding="utf-8")
        (d1 / "db-prod.yaml").write_text("timeout: 30", encoding="utf-8")
        (d2 / "db-prod.yaml").write_text("timeout: 60", encoding="utf-8")
        (d2 / "db-new.yaml").write_text("new: true", encoding="utf-8")

        reports = dd.analyze_drift(
            [str(d1), str(d2)], labels=["A", "B"],
        )
        summary = dd.build_summary(reports)

        # Normalize for snapshot stability
        summary["timestamp"] = "2025-01-15T10:00:00+00:00"
        # Normalize SHA hashes (content-dependent but temp-file paths differ)
        for pair in summary["pairs"]:
            for item in pair["items"]:
                if "source_sha" in item:
                    item["source_sha"] = "SHA_PLACEHOLDER"
                if "target_sha" in item:
                    item["target_sha"] = "SHA_PLACEHOLDER"
        _assert_snapshot("drift_detect_summary", summary)

    def test_empty_summary(self, tmp_path):
        """完全相同目錄的摘要。"""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "db-a.yaml").write_text("same: true", encoding="utf-8")
        (d2 / "db-a.yaml").write_text("same: true", encoding="utf-8")

        reports = dd.analyze_drift([str(d1), str(d2)])
        summary = dd.build_summary(reports)
        summary["timestamp"] = "2025-01-15T10:00:00+00:00"
        _assert_snapshot("drift_detect_empty", summary)


# ── Bilingual Content Snapshots ───────────────────────────────


class TestBilingualContentSnapshot:
    """check_bilingual_content 輸出結構快照。"""

    def test_json_report_structure(self):
        """format_json_report() 結構穩定。"""
        findings = [
            ("warning", "test.en.md: 50% CJK content", "test.en.md", 0.5),
            ("info", "other.md: only 3% CJK", "other.md", 0.03),
        ]
        output = json.loads(cbc.format_json_report(findings))
        _assert_snapshot("bilingual_content_report", output)

    def test_empty_report(self):
        """無 findings 的 JSON 結構。"""
        output = json.loads(cbc.format_json_report([]))
        _assert_snapshot("bilingual_content_empty", output)
