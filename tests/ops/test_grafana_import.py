"""Tests for grafana_import.py — Grafana dashboard ConfigMap importer.

Audit flagged this as a 0% covered MUTATING tool (calls kubectl apply).
Tests cover every code path with subprocess monkeypatched so no real
kubectl is invoked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import grafana_import as gi  # noqa: E402


# ---------------------------------------------------------------------------
# auto_name — pure helper
# ---------------------------------------------------------------------------
class TestAutoName:
    def test_basic(self):
        assert gi.auto_name("foo.json") == "grafana-foo"

    def test_strips_directory(self):
        assert gi.auto_name("/some/path/dashboard.json") == "grafana-dashboard"

    def test_normalises_underscores_and_spaces(self):
        # Both '_' and ' ' become '-' for k8s name safety.
        assert gi.auto_name("my_dashboard name.json") == "grafana-my-dashboard-name"

    def test_lowercases(self):
        assert gi.auto_name("UPPER.json") == "grafana-upper"


# ---------------------------------------------------------------------------
# run_cmd — subprocess wrapper
# ---------------------------------------------------------------------------
class TestRunCmd:
    def test_rejects_string_argument(self):
        with pytest.raises(TypeError, match="list argument"):
            gi.run_cmd("kubectl get pods")

    def test_dry_run_returns_sentinel_without_executing(self, capsys):
        result = gi.run_cmd(["kubectl", "get", "pods"], dry_run=True)
        assert result == "[dry-run]"
        out = capsys.readouterr().out
        assert "[DRY RUN]" in out
        assert "kubectl get pods" in out

    def test_success_returns_stripped_stdout(self, monkeypatch):
        monkeypatch.setattr(
            gi.subprocess, "check_output",
            lambda *a, **kw: "  result-line\n",
        )
        assert gi.run_cmd(["kubectl", "get"]) == "result-line"

    def test_failure_returns_none(self, monkeypatch):
        def boom(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "kubectl", stderr="err")
        monkeypatch.setattr(gi.subprocess, "check_output", boom)
        assert gi.run_cmd(["kubectl", "get"]) is None


# ---------------------------------------------------------------------------
# import_dashboard — mutating path
# ---------------------------------------------------------------------------
class TestImportDashboard:
    def test_missing_file_returns_fail_result(self, tmp_path):
        ghost = tmp_path / "ghost.json"
        results = gi.import_dashboard(str(ghost), "cm", "ns")
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert "File not found" in results[0]["detail"]

    def test_invalid_json_returns_fail_result(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json", encoding="utf-8")
        results = gi.import_dashboard(str(f), "cm", "ns")
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert "Invalid JSON" in results[0]["detail"]

    def test_dry_run_does_not_execute_kubectl(self, tmp_path, monkeypatch, capsys):
        # Real subprocess.check_output should NEVER be called in dry-run.
        def fail_if_called(*args, **kwargs):
            raise AssertionError("subprocess invoked in dry-run mode")
        monkeypatch.setattr(gi.subprocess, "check_output", fail_if_called)
        monkeypatch.setattr(gi.subprocess, "run", fail_if_called)

        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "Test Dashboard"}), encoding="utf-8")
        results = gi.import_dashboard(str(f), "grafana-dash", "monitoring", dry_run=True)
        statuses = [r["status"] for r in results]
        assert "dry-run" in statuses
        assert all(s != "fail" for s in statuses)

    def test_create_configmap_failure_short_circuits(self, tmp_path, monkeypatch):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")

        def boom(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "kubectl")
        monkeypatch.setattr(gi.subprocess, "check_output", boom)
        results = gi.import_dashboard(str(f), "cm", "ns")
        # Only the create failure is recorded; label step is skipped.
        assert len(results) == 1
        assert results[0]["action"] == "create_configmap"
        assert results[0]["status"] == "fail"

    def test_happy_path_creates_then_labels(self, tmp_path, monkeypatch):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "Happy"}), encoding="utf-8")

        # First check_output (generate YAML) succeeds; second (label) succeeds.
        monkeypatch.setattr(gi.subprocess, "check_output",
                            lambda *a, **kw: "kind: ConfigMap\n")
        # subprocess.run for `kubectl apply -f -` succeeds.
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        monkeypatch.setattr(gi.subprocess, "run", lambda *a, **kw: proc)

        results = gi.import_dashboard(str(f), "cm", "monitoring")
        actions = [r["action"] for r in results]
        statuses = [r["status"] for r in results]
        assert "create_configmap" in actions
        assert "label_configmap" in actions
        assert all(s == "pass" for s in statuses)

    def test_apply_failure_short_circuits(self, tmp_path, monkeypatch):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")

        monkeypatch.setattr(gi.subprocess, "check_output",
                            lambda *a, **kw: "kind: ConfigMap\n")
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "apply error"
        monkeypatch.setattr(gi.subprocess, "run", lambda *a, **kw: proc)

        results = gi.import_dashboard(str(f), "cm", "ns")
        # Apply fails, label should not be attempted.
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert "apply" in results[0]["detail"].lower()


# ---------------------------------------------------------------------------
# verify_dashboards — kubectl get parser
# ---------------------------------------------------------------------------
class TestVerifyDashboards:
    def test_kubectl_failure_returns_fail_check(self, monkeypatch):
        monkeypatch.setattr(gi, "run_cmd", lambda *a, **kw: None)
        checks = gi.verify_dashboards("monitoring")
        assert len(checks) == 1
        assert checks[0]["status"] == "fail"

    def test_invalid_json_returns_fail(self, monkeypatch):
        monkeypatch.setattr(gi, "run_cmd", lambda *a, **kw: "{not json")
        checks = gi.verify_dashboards("monitoring")
        assert len(checks) == 1
        assert checks[0]["status"] == "fail"
        assert "parse" in checks[0]["detail"].lower()

    def test_no_items_returns_warning(self, monkeypatch):
        monkeypatch.setattr(gi, "run_cmd",
                            lambda *a, **kw: json.dumps({"items": []}))
        checks = gi.verify_dashboards("monitoring")
        assert len(checks) == 1
        assert checks[0]["status"] == "warn"
        assert "No ConfigMaps" in checks[0]["detail"]

    def test_valid_items_pass(self, monkeypatch):
        kubectl_output = json.dumps({
            "items": [
                {
                    "metadata": {"name": "grafana-dash"},
                    "data": {
                        "dash.json": json.dumps({
                            "title": "My Dashboard",
                            "panels": [{}, {}, {}],
                        }),
                    },
                },
            ],
        })
        monkeypatch.setattr(gi, "run_cmd", lambda *a, **kw: kubectl_output)
        checks = gi.verify_dashboards("monitoring")
        assert len(checks) == 1
        assert checks[0]["status"] == "pass"
        assert "My Dashboard" in checks[0]["detail"]
        assert "3 panels" in checks[0]["detail"]

    def test_invalid_dashboard_json_inside_configmap_fails(self, monkeypatch):
        kubectl_output = json.dumps({
            "items": [
                {"metadata": {"name": "bad"}, "data": {"x.json": "{not json"}},
            ],
        })
        monkeypatch.setattr(gi, "run_cmd", lambda *a, **kw: kubectl_output)
        checks = gi.verify_dashboards("monitoring")
        assert any(c["status"] == "fail" for c in checks)


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------
class TestMain:
    def test_no_args_errors_and_exits(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["grafana_import.py"])
        with pytest.raises(SystemExit) as exc:
            gi.main()
        # parser.error() exits 2.
        assert exc.value.code == 2

    def test_verify_pass_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "x", "status": "pass", "detail": "ok"}])
        monkeypatch.setattr(sys, "argv", ["grafana_import.py", "--verify", "--namespace", "monitoring"])
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 0

    def test_verify_fail_exits_one(self, monkeypatch):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "x", "status": "fail", "detail": "broken"}])
        monkeypatch.setattr(sys, "argv", ["grafana_import.py", "--verify"])
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 1

    def test_verify_json_output(self, monkeypatch, capsys):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "c", "status": "pass", "detail": "d"}])
        monkeypatch.setattr(sys, "argv", ["grafana_import.py", "--verify", "--json"])
        with pytest.raises(SystemExit):
            gi.main()
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["tool"] == "grafana-import"
        assert payload["mode"] == "verify"
        assert payload["status"] == "pass"

    def test_dashboard_dir_not_found_exits_one(self, monkeypatch, tmp_path, capsys):
        ghost = tmp_path / "no-such-dir"
        monkeypatch.setattr(sys, "argv",
                            ["grafana_import.py", "--dashboard-dir", str(ghost)])
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err.lower()

    def test_dashboard_dir_no_jsons_exits_one(self, monkeypatch, tmp_path, capsys):
        d = tmp_path / "empty-dir"
        d.mkdir()
        (d / "readme.txt").write_text("x", encoding="utf-8")  # not .json
        monkeypatch.setattr(sys, "argv",
                            ["grafana_import.py", "--dashboard-dir", str(d)])
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "No dashboard files" in err

    def test_single_dashboard_dry_run_exits_zero(self, monkeypatch, tmp_path, capsys):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["grafana_import.py", "--dashboard", str(f), "--dry-run"])
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
