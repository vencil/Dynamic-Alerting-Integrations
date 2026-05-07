"""Tests for run_chaos_soak.py — v2.8.0 readiness chaos soak harness.

Audit flagged 0% coverage. The pure functions (parse_metrics,
trigger_reload) are easy to test directly. fetch_metrics needs
urllib.request mocked. main() is the orchestrator (skipped here —
its loop is time-bound and best exercised end-to-end).
"""
from __future__ import annotations

import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import run_chaos_soak as rcs  # noqa: E402


# ---------------------------------------------------------------------------
# parse_metrics — pure helper
# ---------------------------------------------------------------------------
class TestParseMetrics:
    def test_empty_input_returns_empty_dict(self):
        assert rcs.parse_metrics("") == {}

    def test_extracts_tracked_metrics(self):
        text = (
            "# HELP go_goroutines Number of goroutines\n"
            "# TYPE go_goroutines gauge\n"
            "go_goroutines 42\n"
            "go_memstats_alloc_bytes 1234567\n"
        )
        out = rcs.parse_metrics(text)
        assert out["go_goroutines"] == 42.0
        assert out["go_memstats_alloc_bytes"] == 1234567.0

    def test_skips_comment_lines(self):
        text = "# go_goroutines is a comment line\ngo_goroutines 5\n"
        assert rcs.parse_metrics(text) == {"go_goroutines": 5.0}

    def test_skips_labeled_samples(self):
        # Labeled samples (with `{label=...}`) are skipped — we want
        # process-level singletons.
        text = (
            'go_goroutines 10\n'
            'go_goroutines{tenant="a"} 5\n'
            'go_goroutines{tenant="b"} 5\n'
        )
        out = rcs.parse_metrics(text)
        assert out["go_goroutines"] == 10.0  # only the unlabeled

    def test_ignores_unknown_metric_names(self):
        text = "irrelevant_metric 999\ngo_goroutines 5\n"
        out = rcs.parse_metrics(text)
        assert "irrelevant_metric" not in out
        assert out["go_goroutines"] == 5.0

    def test_skips_invalid_float_values(self):
        text = "go_goroutines NotANumber\ngo_memstats_sys_bytes 1024\n"
        out = rcs.parse_metrics(text)
        assert "go_goroutines" not in out
        assert out["go_memstats_sys_bytes"] == 1024.0

    def test_skips_short_lines(self):
        # Line with only metric name (no value) is skipped.
        text = "go_goroutines\ngo_memstats_sys_bytes 100\n"
        out = rcs.parse_metrics(text)
        assert "go_goroutines" not in out
        assert out["go_memstats_sys_bytes"] == 100.0

    def test_skips_blank_lines(self):
        text = "go_goroutines 5\n\n\ngo_memstats_sys_bytes 100\n"
        out = rcs.parse_metrics(text)
        assert len(out) == 2


# ---------------------------------------------------------------------------
# fetch_metrics — urllib wrapper
# ---------------------------------------------------------------------------
class TestFetchMetrics:
    def test_success_returns_parsed_dict(self, monkeypatch):
        body = b"go_goroutines 7\n"

        class FakeResp:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr(rcs.urllib.request, "urlopen",
                            lambda url, timeout: FakeResp())
        out = rcs.fetch_metrics("http://localhost:8080")
        assert out == {"go_goroutines": 7.0}

    def test_url_error_returns_none(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise urllib.error.URLError("connection refused")
        monkeypatch.setattr(rcs.urllib.request, "urlopen", boom)
        assert rcs.fetch_metrics("http://localhost:8080") is None
        # Warning should land on stderr.
        err = capsys.readouterr().err
        assert "warn" in err.lower()

    def test_timeout_returns_none(self, monkeypatch):
        def boom(*args, **kwargs):
            raise TimeoutError("timed out")
        monkeypatch.setattr(rcs.urllib.request, "urlopen", boom)
        assert rcs.fetch_metrics("http://localhost:8080", timeout_sec=1.0) is None

    def test_oserror_returns_none(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("network down")
        monkeypatch.setattr(rcs.urllib.request, "urlopen", boom)
        assert rcs.fetch_metrics("http://localhost:8080") is None

    def test_strips_trailing_slash_from_url(self, monkeypatch):
        captured = {}

        class FakeResp:
            def read(self):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(url, timeout):
            captured["url"] = url
            return FakeResp()

        monkeypatch.setattr(rcs.urllib.request, "urlopen", fake_urlopen)
        rcs.fetch_metrics("http://localhost:8080/")
        assert captured["url"] == "http://localhost:8080/metrics"


# ---------------------------------------------------------------------------
# trigger_reload — filesystem mtime/content toggle
# ---------------------------------------------------------------------------
class TestTriggerReload:
    def test_missing_dir_returns_false(self, tmp_path):
        ghost = tmp_path / "no-such-dir"
        assert rcs.trigger_reload(ghost) is False

    def test_no_yaml_files_returns_false(self, tmp_path):
        # Directory exists but contains no .yaml files.
        (tmp_path / "readme.md").write_text("x", encoding="utf-8")
        assert rcs.trigger_reload(tmp_path) is False

    def test_only_underscore_files_returns_false(self, tmp_path):
        # _defaults.yaml is intentionally skipped (platform invariant).
        # If only underscore-prefixed files exist, no eligible target → False.
        (tmp_path / "_defaults.yaml").write_text("x: 1\n", encoding="utf-8")
        assert rcs.trigger_reload(tmp_path) is False

    def test_appends_marker_on_first_call(self, tmp_path):
        f = tmp_path / "tenant-a.yaml"
        f.write_text("x: 1\n", encoding="utf-8")
        assert rcs.trigger_reload(tmp_path) is True
        new = f.read_text(encoding="utf-8")
        assert "soak-toggle: A" in new

    def test_toggles_a_to_b_on_second_call(self, tmp_path):
        f = tmp_path / "tenant-a.yaml"
        f.write_text("x: 1\n# soak-toggle: A\n", encoding="utf-8")
        rcs.trigger_reload(tmp_path)
        toggled = f.read_text(encoding="utf-8")
        assert "soak-toggle: B" in toggled
        assert "soak-toggle: A" not in toggled

    def test_toggles_b_to_a(self, tmp_path):
        f = tmp_path / "tenant-a.yaml"
        f.write_text("x: 1\n# soak-toggle: B\n", encoding="utf-8")
        rcs.trigger_reload(tmp_path)
        toggled = f.read_text(encoding="utf-8")
        assert "soak-toggle: A" in toggled
        assert "soak-toggle: B" not in toggled

    def test_skips_underscore_files_picks_other(self, tmp_path):
        # _defaults.yaml is skipped; tenant-a.yaml is perturbed.
        defaults = tmp_path / "_defaults.yaml"
        defaults.write_text("baseline: 1\n", encoding="utf-8")
        tenant = tmp_path / "tenant-a.yaml"
        tenant.write_text("x: 1\n", encoding="utf-8")
        assert rcs.trigger_reload(tmp_path) is True
        # _defaults.yaml is unchanged; tenant-a.yaml has the marker.
        assert defaults.read_text(encoding="utf-8") == "baseline: 1\n"
        assert "soak-toggle" in tenant.read_text(encoding="utf-8")

    def test_walks_subdirectories(self, tmp_path):
        # rglob — nested files are eligible.
        sub = tmp_path / "sub"
        sub.mkdir()
        f = sub / "tenant-x.yaml"
        f.write_text("x: 1\n", encoding="utf-8")
        assert rcs.trigger_reload(tmp_path) is True
        assert "soak-toggle" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# RunConfig dataclass — smoke
# ---------------------------------------------------------------------------
class TestRunConfig:
    def test_defaults_for_optional_fields(self):
        cfg = rcs.RunConfig(
            target_url="http://x",
            config_dir="/tmp",
            duration_min=1,
            reload_interval_sec=10,
            metrics_poll_sec=5,
            output_dir="/out",
        )
        assert cfg.started_at_utc == ""
        assert cfg.ended_at_utc == ""
        assert cfg.reload_count == 0
        assert cfg.poll_count == 0


# ---------------------------------------------------------------------------
# main — caller-error surfaces (avoids the long soak loop)
# ---------------------------------------------------------------------------
class TestMainCallerErrors:
    def test_missing_config_dir_returns_one(self, monkeypatch, tmp_path, capsys):
        out_dir = tmp_path / "out"
        ghost = tmp_path / "ghost-config"
        monkeypatch.setattr(sys, "argv", [
            "run_chaos_soak.py",
            "--target-url", "http://localhost:8080",
            "--config-dir", str(ghost),
            "--output-dir", str(out_dir),
            "--duration-min", "1",
        ])
        # main() returns 1 on missing config dir before the soak loop.
        assert rcs.main() == 1
        err = capsys.readouterr().err
        assert "config-dir not found" in err

    def test_unreachable_target_returns_one(self, monkeypatch, tmp_path, capsys):
        config_dir = tmp_path / "conf"
        config_dir.mkdir()
        out_dir = tmp_path / "out"
        # fetch_metrics returns None → "cannot reach" → exit 1.
        monkeypatch.setattr(rcs, "fetch_metrics", lambda *a, **kw: None)
        monkeypatch.setattr(sys, "argv", [
            "run_chaos_soak.py",
            "--target-url", "http://localhost:8080",
            "--config-dir", str(config_dir),
            "--output-dir", str(out_dir),
            "--duration-min", "1",
        ])
        assert rcs.main() == 1
        err = capsys.readouterr().err
        assert "cannot reach" in err

    def test_zero_duration_completes_with_empty_metrics(self, monkeypatch, tmp_path):
        # Empty initial metrics is a WARN, not a hard error. Pair it with
        # `--duration-min 0` so the soak loop exits immediately on the first
        # `time.time() < end_at` check — verifies the harness completes
        # cleanly without fetching/looping.
        config_dir = tmp_path / "conf"
        config_dir.mkdir()
        out_dir = tmp_path / "out"
        monkeypatch.setattr(rcs, "fetch_metrics", lambda *a, **kw: {})
        monkeypatch.setattr(sys, "argv", [
            "run_chaos_soak.py",
            "--target-url", "http://localhost:8080",
            "--config-dir", str(config_dir),
            "--output-dir", str(out_dir),
            "--duration-min", "0",
        ])
        # `--duration-min 0` → loop never executes → returns 0 cleanly.
        assert rcs.main() == 0
        # Output files were created in the finally block.
        assert (out_dir / "summary.txt").exists()
        assert (out_dir / "metrics-timeseries.csv").exists()
        assert (out_dir / "run-config.json").exists()
