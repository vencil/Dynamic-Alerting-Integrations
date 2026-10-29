"""Tests for grafana_import.py — Grafana dashboard ConfigMap importer.

Audit flagged this as a 0% covered MUTATING tool (calls kubectl apply).
Tests cover every code path with subprocess monkeypatched so no real
kubectl is invoked.
"""
from __future__ import annotations

import errno
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
from _lib_exitcodes import EXIT_CALLER_ERROR, CallerError  # noqa: E402
import _lib_kubectl  # noqa: E402
from _kubectl_fake import kubectl_run  # noqa: E402  (#3349)
from _lib_kubectl import KubectlError  # noqa: E402


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
        # #1112: the echoed command is progress prose → stderr, so that
        # `--dry-run --json` leaves stdout holding exactly one JSON document.
        result = gi.run_cmd(["kubectl", "get", "pods"], dry_run=True)
        assert result == "[dry-run]"
        captured = capsys.readouterr()
        assert "[DRY RUN]" in captured.err
        assert "kubectl get pods" in captured.err
        assert captured.out == ""

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

    @pytest.mark.parametrize("raised, why", [
        (FileNotFoundError(errno.ENOENT, "No such file or directory", "kubectl"),
         "kubectl is not on PATH"),
        (subprocess.TimeoutExpired("kubectl", 120), "no answer within 120s"),
    ], ids=["not-on-path", "timeout"])
    def test_no_kubectl_or_no_answer_is_kubectl_error(self, monkeypatch, raised, why):
        """#3349: the environment, named (rc 2, reason kubectl_error) — these
        escaped as a traceback before; a kubectl that answered non-zero is
        still None (the caller's own fail row)."""
        def boom(*args, **kwargs):
            raise raised
        monkeypatch.setattr(gi.subprocess, "check_output", boom)
        with pytest.raises(KubectlError, match=why) as exc:
            gi.run_cmd(["kubectl", "create", "configmap", "x"])
        assert exc.value.reason == "kubectl_error"
        assert "kubectl create configmap" in str(exc.value)


# ---------------------------------------------------------------------------
# import_dashboard — mutating path
# ---------------------------------------------------------------------------
class TestImportDashboard:
    def test_missing_file_returns_fail_result(self, tmp_path):
        ghost = tmp_path / "ghost.json"
        results = gi.import_dashboard(str(ghost), "cm", "ns")
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert results[0]["detail"] == f"--dashboard {ghost}: {os.strerror(errno.ENOENT)}"

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
def _kubectl_lists(monkeypatch, rc=0, stdout="", stderr=""):
    """The `_lib_kubectl` runner's kubectl answers as given; returns the
    recorded argv list (#3349: verify reads through `kubectl_list`)."""
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    monkeypatch.setattr(_lib_kubectl.subprocess, "run", kubectl_run(fake))
    return calls


class TestVerifyDashboards:
    def test_kubectl_failure_raises_kubectl_error(self, monkeypatch):
        """#3349: a list that got no answer is KubectlError (rc 2 at run()'s
        exit), not a `caller_error` fail row beside the dashboard checks."""
        _kubectl_lists(monkeypatch, rc=1, stdout=json.dumps({"kind": "List", "items": []}),
                       stderr="Error from server (Forbidden): configmaps is forbidden")
        with pytest.raises(KubectlError, match=r"\(exit 1\).*Forbidden") as exc:
            gi.verify_dashboards("monitoring")
        assert exc.value.reason == "kubectl_error"

    def test_invalid_json_raises_kubectl_error(self, monkeypatch):
        _kubectl_lists(monkeypatch, stdout="{not json")
        with pytest.raises(KubectlError, match="not JSON"):
            gi.verify_dashboards("monitoring")

    def test_no_items_returns_warning(self, monkeypatch):
        calls = _kubectl_lists(monkeypatch, stdout=json.dumps({"items": []}))
        checks = gi.verify_dashboards("monitoring")
        assert len(checks) == 1
        assert checks[0]["status"] == "warn"
        assert "No ConfigMaps" in checks[0]["detail"]
        assert calls == [["kubectl", "get", "configmap", "-n", "monitoring",
                          "-l", "grafana_dashboard=1", "-o", "json"]]

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
        _kubectl_lists(monkeypatch, stdout=kubectl_output)
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
        _kubectl_lists(monkeypatch, stdout=kubectl_output)
        checks = gi.verify_dashboards("monitoring")
        assert any(c["status"] == "fail" for c in checks)


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------
class TestMain:
    def test_no_args_errors_and_exits(self, monkeypatch, cli_argv):
        cli_argv("grafana_import.py")
        with pytest.raises(SystemExit) as exc:
            gi.main()
        # parser.error() exits 2.
        assert exc.value.code == 2

    def test_verify_pass_exits_zero(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "x", "status": "pass", "detail": "ok"}])
        cli_argv("grafana_import.py", "--verify", "--namespace", "monitoring")
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 0

    def test_verify_fail_exits_one(self, monkeypatch, cli_argv):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "x", "status": "fail", "detail": "broken"}])
        cli_argv("grafana_import.py", "--verify")
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 1

    def test_verify_json_output(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "c", "status": "pass", "detail": "d"}])
        cli_argv("grafana_import.py", "--verify", "--json")
        with pytest.raises(SystemExit):
            gi.main()
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["tool"] == "grafana-import"
        assert payload["mode"] == "verify"
        assert payload["status"] == "pass"

    def test_dashboard_dir_not_found_exits_caller_error(self, monkeypatch, tmp_path, capsys, cli_argv):
        ghost = tmp_path / "no-such-dir"
        cli_argv("grafana_import.py", "--dashboard-dir", str(ghost))
        with pytest.raises(CallerError) as exc:  # rc 2 at run()'s exit (#3276)
            gi.main()
        assert exc.value.reason == "dir_not_found"
        assert str(ghost) in str(exc.value)

    def test_dashboard_dir_no_jsons_exits_caller_error(self, monkeypatch, tmp_path, capsys, cli_argv):
        d = tmp_path / "empty-dir"
        d.mkdir()
        (d / "readme.txt").write_text("x", encoding="utf-8")  # not .json
        cli_argv("grafana_import.py", "--dashboard-dir", str(d))
        with pytest.raises(SystemExit) as exc:
            gi.main()
        # #452: empty dir = caller error (2), consistent with dir-not-found
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "No dashboard files" in err

    def test_single_dashboard_dry_run_exits_zero(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        cli_argv("grafana_import.py", "--dashboard", str(f), "--dry-run")
        with pytest.raises(SystemExit) as exc:
            gi.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out


# ---------------------------------------------------------------------------
# --verify through the real CLI, kubectl without an answer (#3349)
# ---------------------------------------------------------------------------
def _verify_cli(tmp_path, path):
    return subprocess.run(
        [sys.executable, os.path.join(_TOOLS_DIR, "grafana_import.py"), "--verify", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(tmp_path),
        env={**os.environ, "PATH": path})


class TestVerifyWithoutAnAnswer:
    """`--verify --json` when kubectl gives no answer: rc 2, one envelope with
    reason `kubectl_error`, no traceback — not a verify report."""

    def test_forbidden_kubectl(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        exe = bindir / "kubectl"
        exe.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stdout.write('{\"apiVersion\": \"v1\", \"kind\": \"List\", \"items\": []}\\n')\n"
            "sys.stderr.write('Error from server (Forbidden): configmaps is forbidden: '\n"
            "                 'User \"nobody\" cannot list resource \"configmaps\"\\n')\n"
            "sys.exit(1)\n", encoding="utf-8")
        exe.chmod(0o755)
        r = _verify_cli(tmp_path, f"{bindir}{os.pathsep}{os.environ['PATH']}")
        assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
        assert "Traceback" not in r.stderr
        doc = json.loads(r.stdout)
        assert doc["reason"] == "kubectl_error", doc
        assert "Forbidden" in doc["error"]
        assert "checks" not in doc   # no verify report under the envelope

    def test_no_kubectl_on_path(self, tmp_path):
        empty = tmp_path / "no-kubectl"
        empty.mkdir()
        r = _verify_cli(tmp_path, str(empty))
        assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
        assert "Traceback" not in r.stderr
        doc = json.loads(r.stdout)
        assert doc["reason"] == "kubectl_error", doc
        assert "kubectl is not on PATH" in doc["error"]
