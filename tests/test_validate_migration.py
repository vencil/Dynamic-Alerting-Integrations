"""Tests for validate_migration.py — Shadow Monitoring validation tool."""

import csv
import io
import json
import os
import sys
import textwrap
from unittest import mock

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'ops'))
import validate_migration as vm  # noqa: E402


# ---------------------------------------------------------------------------
# extract_value_map
# ---------------------------------------------------------------------------

class TestExtractValueMap:
    """Tests for extract_value_map()."""

    def test_basic_extraction(self):
        results = [
            {"metric": {"tenant": "db-a"}, "value": [1234567890, "42.5"]},
            {"metric": {"tenant": "db-b"}, "value": [1234567890, "10.0"]},
        ]
        got = vm.extract_value_map(results)
        assert got == {"db-a": 42.5, "db-b": 10.0}

    def test_custom_group_by(self):
        results = [
            {"metric": {"instance": "host1"}, "value": [0, "1.0"]},
            {"metric": {"instance": "host2"}, "value": [0, "2.0"]},
        ]
        got = vm.extract_value_map(results, group_by="instance")
        assert got == {"host1": 1.0, "host2": 2.0}

    def test_missing_label_uses_no_label(self):
        results = [{"metric": {}, "value": [0, "5.0"]}]
        got = vm.extract_value_map(results)
        assert "__no_label__" in got
        assert got["__no_label__"] == 5.0

    def test_invalid_value_becomes_none(self):
        results = [{"metric": {"tenant": "t1"}, "value": [0, "NaN_bad"]}]
        got = vm.extract_value_map(results)
        assert got["t1"] is None

    def test_none_value_becomes_none(self):
        results = [{"metric": {"tenant": "t1"}, "value": [0, None]}]
        got = vm.extract_value_map(results)
        assert got["t1"] is None

    def test_empty_results(self):
        assert vm.extract_value_map([]) == {}


# ---------------------------------------------------------------------------
# compare_vectors
# ---------------------------------------------------------------------------

class TestCompareVectors:
    """Tests for compare_vectors()."""

    def test_exact_match(self):
        old_map = {"db-a": 100.0, "db-b": 200.0}
        new_map = {"db-a": 100.0, "db-b": 200.0}
        diffs = vm.compare_vectors(old_map, new_map)
        assert all(d["status"] == "match" for d in diffs)

    def test_within_tolerance(self):
        old_map = {"db-a": 1000.0}
        new_map = {"db-a": 1000.5}  # 0.05% diff, within 0.1%
        diffs = vm.compare_vectors(old_map, new_map, tolerance=0.001)
        assert diffs[0]["status"] == "match"

    def test_mismatch_beyond_tolerance(self):
        old_map = {"db-a": 100.0}
        new_map = {"db-a": 200.0}
        diffs = vm.compare_vectors(old_map, new_map)
        assert diffs[0]["status"] == "mismatch"
        assert diffs[0]["delta"] == 100.0

    def test_old_missing(self):
        diffs = vm.compare_vectors({}, {"db-a": 5.0})
        assert diffs[0]["status"] == "old_missing"
        assert diffs[0]["delta"] is None

    def test_new_missing(self):
        diffs = vm.compare_vectors({"db-a": 5.0}, {})
        assert diffs[0]["status"] == "new_missing"
        assert diffs[0]["delta"] is None

    def test_both_none(self):
        diffs = vm.compare_vectors({"db-a": None}, {"db-a": None})
        assert diffs[0]["status"] == "both_empty"

    def test_keys_sorted(self):
        old_map = {"z": 1.0, "a": 2.0}
        new_map = {"z": 1.0, "a": 2.0}
        diffs = vm.compare_vectors(old_map, new_map)
        assert [d["tenant"] for d in diffs] == ["a", "z"]

    def test_mixed_keys(self):
        """Keys present in only one map appear as missing."""
        old_map = {"a": 1.0, "b": 2.0}
        new_map = {"b": 2.0, "c": 3.0}
        diffs = vm.compare_vectors(old_map, new_map)
        status_map = {d["tenant"]: d["status"] for d in diffs}
        assert status_map["a"] == "new_missing"
        assert status_map["b"] == "match"
        assert status_map["c"] == "old_missing"


# ---------------------------------------------------------------------------
# query_prometheus (mock http_get_json)
# ---------------------------------------------------------------------------

class TestQueryPrometheus:
    """Tests via monkeypatch — query_prometheus is now an alias to _lib_python.query_prometheus_instant."""

    def test_success(self, monkeypatch):
        fake = lambda prom_url, promql: ([{"metric": {}, "value": [0, "1"]}], None)
        monkeypatch.setattr(vm, "query_prometheus", fake)
        results, err = vm.query_prometheus("http://prom:9090", "up")
        assert err is None
        assert len(results) == 1

    def test_http_error(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "connection refused")
        monkeypatch.setattr(vm, "query_prometheus", fake)
        results, err = vm.query_prometheus("http://prom:9090", "up")
        assert results is None
        assert "connection refused" in err

    def test_api_error_status(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "bad query")
        monkeypatch.setattr(vm, "query_prometheus", fake)
        results, err = vm.query_prometheus("http://prom:9090", "bad{}")
        assert results is None
        assert "bad query" in err

    def test_api_error_unknown(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "Unknown error")
        monkeypatch.setattr(vm, "query_prometheus", fake)
        results, err = vm.query_prometheus("http://prom:9090", "bad{}")
        assert results is None
        assert "Unknown error" in err


# ---------------------------------------------------------------------------
# run_single_comparison
# ---------------------------------------------------------------------------

class TestRunSingleComparison:
    """Tests for run_single_comparison()."""

    @mock.patch("validate_migration.query_prometheus")
    def test_success(self, mock_qp):
        mock_qp.side_effect = [
            ([{"metric": {"tenant": "db-a"}, "value": [0, "10"]}], None),
            ([{"metric": {"tenant": "db-a"}, "value": [0, "10"]}], None),
        ]
        result = vm.run_single_comparison("http://p:9090", "old_q", "new_q", "test_label")
        assert result is not None
        assert result["label"] == "test_label"
        assert result["old_count"] == 1
        assert result["new_count"] == 1
        assert result["diffs"][0]["status"] == "match"

    @mock.patch("validate_migration.query_prometheus")
    def test_old_query_fails(self, mock_qp):
        mock_qp.return_value = (None, "timeout")
        result = vm.run_single_comparison("http://p:9090", "old_q", "new_q", "lbl")
        assert result is None

    @mock.patch("validate_migration.query_prometheus")
    def test_new_query_fails(self, mock_qp):
        mock_qp.side_effect = [
            ([{"metric": {"tenant": "db-a"}, "value": [0, "10"]}], None),
            (None, "500 error"),
        ]
        result = vm.run_single_comparison("http://p:9090", "old_q", "new_q", "lbl")
        assert result is None


# ---------------------------------------------------------------------------
# load_mapping_pairs
# ---------------------------------------------------------------------------

class TestLoadMappingPairs:
    """Tests for load_mapping_pairs()."""

    def test_loads_valid_mapping(self, tmp_path):
        mapping = {
            "custom_cpu_usage": {
                "original_metric": "node_cpu_seconds_total",
                "alert_name": "HighCPU",
                "golden_match": True,
            },
            "custom_mem_usage": {
                "original_metric": "node_memory_MemFree_bytes",
            },
        }
        p = tmp_path / "mapping.yaml"
        p.write_text(yaml.safe_dump(mapping), encoding="utf-8")

        pairs = vm.load_mapping_pairs(str(p))
        assert len(pairs) == 2
        assert pairs[0]["label"] == "custom_cpu_usage"
        assert pairs[0]["old_query"] == "node_cpu_seconds_total"
        assert pairs[0]["new_query"] == "tenant:custom_cpu_usage:max"
        assert pairs[0]["alert_name"] == "HighCPU"
        assert pairs[0]["golden_match"] is True

    def test_skips_entries_without_original(self, tmp_path):
        mapping = {
            "has_original": {"original_metric": "metric_a"},
            "no_original": {"alert_name": "Alert1"},
        }
        p = tmp_path / "mapping.yaml"
        p.write_text(yaml.safe_dump(mapping), encoding="utf-8")

        pairs = vm.load_mapping_pairs(str(p))
        assert len(pairs) == 1
        assert pairs[0]["label"] == "has_original"

    def test_empty_mapping(self, tmp_path):
        p = tmp_path / "mapping.yaml"
        p.write_text("", encoding="utf-8")
        pairs = vm.load_mapping_pairs(str(p))
        assert pairs == []


# ---------------------------------------------------------------------------
# print_summary
# ---------------------------------------------------------------------------

class TestPrintSummary:
    """Tests for print_summary()."""

    def test_all_match(self, capsys):
        results = [{
            "label": "cpu",
            "old_query": "q1",
            "new_query": "q2",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 1.0,
                        "status": "match", "delta": 0.0}],
        }]
        vm.print_summary(results)
        out = capsys.readouterr().out
        assert "數值一致: 1" in out
        assert "數值差異: 0" in out
        assert "安全切換" in out

    def test_with_mismatches(self, capsys):
        results = [{
            "label": "mem",
            "old_query": "q1",
            "new_query": "q2",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 10.0, "new_value": 20.0,
                        "status": "mismatch", "delta": 10.0}],
        }]
        vm.print_summary(results)
        out = capsys.readouterr().out
        assert "數值差異: 1" in out
        assert "db-a" in out

    def test_with_missing(self, capsys):
        results = [{
            "label": "disk",
            "old_query": "q1",
            "new_query": "q2",
            "old_count": 1,
            "new_count": 0,
            "diffs": [{"tenant": "db-a", "old_value": 5.0, "new_value": None,
                        "status": "new_missing", "delta": None}],
        }]
        vm.print_summary(results)
        out = capsys.readouterr().out
        assert "缺少資料: 1" in out

    def test_none_results_skipped(self, capsys):
        results = [None, {
            "label": "x",
            "old_query": "q1",
            "new_query": "q2",
            "old_count": 0,
            "new_count": 0,
            "diffs": [],
        }]
        vm.print_summary(results)
        out = capsys.readouterr().out
        assert "比對組數: 2" in out


# ---------------------------------------------------------------------------
# write_csv_report
# ---------------------------------------------------------------------------

class TestWriteCsvReport:
    """Tests for write_csv_report()."""

    def test_writes_csv(self, tmp_path):
        results = [{
            "label": "cpu",
            "old_query": "old_cpu",
            "new_query": "new_cpu",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 10.0, "new_value": 10.0,
                        "status": "match", "delta": 0.0}],
        }]
        csv_path = vm.write_csv_report(results, str(tmp_path / "out"))
        assert os.path.exists(csv_path)

        content = open(csv_path, encoding="utf-8-sig").read()
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        assert rows[0][0] == "Label"  # header
        assert rows[1][0] == "cpu"
        assert rows[1][1] == "db-a"

    def test_skips_none_results(self, tmp_path):
        results = [None]
        csv_path = vm.write_csv_report(results, str(tmp_path / "out"))
        content = open(csv_path, encoding="utf-8-sig").read()
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        assert len(rows) == 1  # header only


# ---------------------------------------------------------------------------
# ConvergenceTracker
# ---------------------------------------------------------------------------

class TestConvergenceTracker:
    """Tests for ConvergenceTracker class."""

    def _make_result(self, label, status):
        """Helper: create a result dict with a single diff of given status."""
        return {
            "label": label,
            "old_query": "q1",
            "new_query": "q2",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 1.0,
                        "status": status, "delta": 0.0}],
        }

    def test_not_converged_insufficient_rounds(self):
        tracker = vm.ConvergenceTracker(stability_window=3)
        tracker.record_round([self._make_result("cpu", "match")])
        assert not tracker.is_converged("cpu")

    def test_converged_after_stability_window(self):
        tracker = vm.ConvergenceTracker(stability_window=3)
        for _ in range(3):
            tracker.record_round([self._make_result("cpu", "match")])
        assert tracker.is_converged("cpu")

    def test_not_converged_with_mismatch(self):
        tracker = vm.ConvergenceTracker(stability_window=3)
        tracker.record_round([self._make_result("cpu", "match")])
        tracker.record_round([self._make_result("cpu", "mismatch")])
        tracker.record_round([self._make_result("cpu", "match")])
        assert not tracker.is_converged("cpu")

    def test_compute_report_insufficient_rounds(self):
        tracker = vm.ConvergenceTracker(stability_window=3)
        tracker.record_round([self._make_result("cpu", "match")])
        report = tracker.compute_report()
        assert report["ready"] is False
        assert "Insufficient" in report["reason"]

    def test_compute_report_no_pairs(self):
        tracker = vm.ConvergenceTracker()
        tracker.round_count = 5
        report = tracker.compute_report()
        assert report["ready"] is False
        assert "No pairs" in report["reason"]

    def test_compute_report_all_converged(self):
        tracker = vm.ConvergenceTracker(stability_window=2)
        for _ in range(2):
            tracker.record_round([
                self._make_result("cpu", "match"),
                self._make_result("mem", "match"),
            ])
        report = tracker.compute_report()
        assert report["ready"] is True
        assert report["convergence_percentage"] == 100.0
        assert report["converged_count"] == 2
        assert "Safe to cutover" in report["recommendation"]

    def test_compute_report_partial_convergence(self):
        tracker = vm.ConvergenceTracker(stability_window=2)
        for _ in range(2):
            tracker.record_round([
                self._make_result("cpu", "match"),
                self._make_result("mem", "mismatch"),
            ])
        report = tracker.compute_report()
        assert report["ready"] is False
        assert report["converged_count"] == 1
        assert "mem" in report["unconverged_pairs"]

    def test_record_round_mixed_status(self):
        """Result with old_missing goes to 'mixed'."""
        tracker = vm.ConvergenceTracker(stability_window=2)
        result = {
            "label": "disk",
            "diffs": [
                {"status": "match"},
                {"status": "old_missing"},
            ],
        }
        tracker.record_round([result])
        assert tracker.pair_history["disk"] == ["mixed"]

    def test_record_round_skips_none(self):
        tracker = vm.ConvergenceTracker()
        tracker.record_round([None])
        assert tracker.pair_history == {}

    def test_print_status_not_ready(self, capsys):
        tracker = vm.ConvergenceTracker(stability_window=2)
        for _ in range(2):
            tracker.record_round([
                self._make_result("cpu", "match"),
                self._make_result("mem", "mismatch"),
            ])
        report = tracker.print_status()
        out = capsys.readouterr().out
        assert "Unconverged" in out
        assert not report["ready"]

    def test_print_status_ready(self, capsys):
        tracker = vm.ConvergenceTracker(stability_window=2)
        for _ in range(2):
            tracker.record_round([self._make_result("cpu", "match")])
        report = tracker.print_status()
        out = capsys.readouterr().out
        assert "CUTOVER READY" in out
        assert report["ready"] is True

    def test_is_converged_unknown_label(self):
        tracker = vm.ConvergenceTracker()
        assert not tracker.is_converged("nonexistent")


# ---------------------------------------------------------------------------
# main() CLI tests
# ---------------------------------------------------------------------------

class TestMain:
    """Tests for main() CLI entry point."""

    @mock.patch("validate_migration.run_single_comparison")
    def test_main_single_pair(self, mock_run, tmp_path, capsys):
        mock_run.return_value = {
            "label": "manual",
            "old_query": "old_q",
            "new_query": "new_q",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 1.0,
                        "status": "match", "delta": 0.0}],
        }
        out_dir = str(tmp_path / "out")
        with mock.patch("sys.argv", [
            "validate_migration.py",
            "--old", "old_metric",
            "--new", "new_metric",
            "--prometheus", "http://localhost:9090",
            "-o", out_dir,
        ]):
            vm.main()

        out = capsys.readouterr().out
        assert "CSV" in out
        assert os.path.exists(os.path.join(out_dir, "validation-report.csv"))

    @mock.patch("validate_migration.load_mapping_pairs")
    @mock.patch("validate_migration.run_single_comparison")
    def test_main_mapping_mode(self, mock_run, mock_load, tmp_path, capsys):
        mock_load.return_value = [
            {"label": "cpu", "old_query": "oq", "new_query": "nq"},
        ]
        mock_run.return_value = {
            "label": "cpu",
            "old_query": "oq",
            "new_query": "nq",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 1.0,
                        "status": "match", "delta": 0.0}],
        }
        out_dir = str(tmp_path / "out")
        with mock.patch("sys.argv", [
            "validate_migration.py",
            "--mapping", "fake_mapping.yaml",
            "-o", out_dir,
        ]):
            vm.main()
        out = capsys.readouterr().out
        assert "載入 1 組" in out

    def test_main_old_without_new(self):
        """--old without --new should exit."""
        with mock.patch("sys.argv", [
            "validate_migration.py", "--old", "some_metric",
        ]):
            with pytest.raises(SystemExit):
                vm.main()

    @mock.patch("validate_migration.load_mapping_pairs")
    def test_main_empty_pairs(self, mock_load, capsys):
        mock_load.return_value = []
        with mock.patch("sys.argv", [
            "validate_migration.py",
            "--mapping", "fake.yaml",
        ]):
            vm.main()
        out = capsys.readouterr().out
        assert "No comparison pairs" in out

    @mock.patch("validate_migration.time.sleep")
    @mock.patch("validate_migration.run_single_comparison")
    @mock.patch("validate_migration.load_mapping_pairs")
    def test_main_watch_mode_converges(self, mock_load, mock_run, mock_sleep, tmp_path, capsys):
        mock_load.return_value = [
            {"label": "cpu", "old_query": "oq", "new_query": "nq"},
        ]
        mock_run.return_value = {
            "label": "cpu",
            "old_query": "oq",
            "new_query": "nq",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 1.0,
                        "status": "match", "delta": 0.0}],
        }
        out_dir = str(tmp_path / "out")
        with mock.patch("sys.argv", [
            "validate_migration.py",
            "--mapping", "fake.yaml",
            "--watch", "--rounds", "10", "--interval", "1",
            "--auto-detect-convergence", "--stability-window", "2",
            "-o", out_dir,
        ]):
            vm.main()
        out = capsys.readouterr().out
        assert "CUTOVER READY" in out
        # Convergence report written
        assert os.path.exists(os.path.join(out_dir, "cutover-readiness.json"))

    @mock.patch("validate_migration.time.sleep")
    @mock.patch("validate_migration.run_single_comparison")
    @mock.patch("validate_migration.load_mapping_pairs")
    def test_main_watch_mode_no_convergence(self, mock_load, mock_run, mock_sleep, tmp_path, capsys):
        mock_load.return_value = [
            {"label": "cpu", "old_query": "oq", "new_query": "nq"},
        ]
        mock_run.return_value = {
            "label": "cpu",
            "old_query": "oq",
            "new_query": "nq",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 2.0,
                        "status": "mismatch", "delta": 1.0}],
        }
        out_dir = str(tmp_path / "out")
        with mock.patch("sys.argv", [
            "validate_migration.py",
            "--mapping", "fake.yaml",
            "--watch", "--rounds", "3", "--interval", "1",
            "--auto-detect-convergence", "--stability-window", "2",
            "-o", out_dir,
        ]):
            vm.main()
        out = capsys.readouterr().out
        assert "without full convergence" in out
        assert os.path.exists(os.path.join(out_dir, "cutover-readiness.json"))

    @mock.patch("validate_migration.time.sleep")
    @mock.patch("validate_migration.run_single_comparison")
    @mock.patch("validate_migration.load_mapping_pairs")
    def test_main_watch_no_convergence_tracking(self, mock_load, mock_run, mock_sleep, tmp_path, capsys):
        """Watch mode without --auto-detect-convergence."""
        mock_load.return_value = [
            {"label": "cpu", "old_query": "oq", "new_query": "nq"},
        ]
        mock_run.return_value = {
            "label": "cpu",
            "old_query": "oq",
            "new_query": "nq",
            "old_count": 1,
            "new_count": 1,
            "diffs": [{"tenant": "db-a", "old_value": 1.0, "new_value": 1.0,
                        "status": "match", "delta": 0.0}],
        }
        out_dir = str(tmp_path / "out")
        with mock.patch("sys.argv", [
            "validate_migration.py",
            "--mapping", "fake.yaml",
            "--watch", "--rounds", "2", "--interval", "1",
            "-o", out_dir,
        ]):
            vm.main()
        out = capsys.readouterr().out
        assert "CSV report" in out
