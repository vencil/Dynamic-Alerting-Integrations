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
from _kubectl_fake import install as install_kubectl  # noqa: E402  (#3349)
from _lib_kubectl import KubectlError  # noqa: E402
from _lib_exitcodes import emit_result, exit_on_crash  # noqa: E402  (#3216: in-process main() → exit status)


def _as_run(result):
    """What `_lib_exitcodes.run` does with main()'s answer (#3216): a Report's
    document is written and its status is the exit, as SystemExit."""
    raise SystemExit(emit_result(result))


def _run_main(main):
    """`_as_run(main())`, plus what `run()` does with a CallerError main
    raises (#3617: a run that answered nothing): the caller-error exit, its
    line on stderr, stdout empty."""
    try:
        result = main()
    except CallerError as exc:
        exit_on_crash(exc)
    _as_run(result)


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
# run_kubectl — every kubectl of an import, through _lib_kubectl (#3539)
# ---------------------------------------------------------------------------
def _kubectl(monkeypatch, answers=None):
    """Every kubectl the import starts answers by verb: *answers* maps a verb
    to `(rc, stdout, stderr)`, to an exception to raise at its start, or to a
    callable taking the argv; a verb not named answers rc 0 with a manifest
    on stdout. Returns the recorded argvs (each starts with `kubectl`)."""
    answers = answers or {}
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        answer = answers.get(cmd[1], (0, "kind: ConfigMap\n", ""))
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(cmd, **kw)
        return subprocess.CompletedProcess(cmd, *answer)
    install_kubectl(monkeypatch, fake)
    return calls


#: What starting kubectl can raise, and the words `_lib_engine` names it by.
_NO_EXIT_STATUS = [
    (PermissionError(errno.EACCES, "Permission denied", "kubectl"), "cannot run kubectl"),
    (OSError(errno.ENOEXEC, "Exec format error", "kubectl"), "cannot run kubectl"),
    (FileNotFoundError(errno.ENOENT, "No such file or directory", "kubectl"), "cannot run kubectl"),
    (subprocess.TimeoutExpired("kubectl", 120), "did not finish within 120s"),
]
_NO_EXIT_IDS = ["eacces", "enoexec", "enoent", "timeout"]


class TestRunKubectl:
    def test_rejects_string_argument(self):
        with pytest.raises(TypeError, match="list argument"):
            gi.run_kubectl("get", "pods")

    def test_dry_run_prints_the_command_without_executing(self, monkeypatch, capsys):
        # #1112: the echoed command is progress prose → stderr, so that
        # `--dry-run --json` leaves stdout holding exactly one JSON document.
        calls = _kubectl(monkeypatch)
        assert gi.run_kubectl("get", ["pods"], dry_run=True) is None
        captured = capsys.readouterr()
        assert "[DRY RUN]" in captured.err
        assert "kubectl get pods" in captured.err
        assert captured.out == ""
        assert calls == []

    def test_an_exit_status_is_returned_for_the_caller_to_read(self, monkeypatch):
        _kubectl(monkeypatch, {"get": (1, "  out\n", "err\n")})
        proc = gi.run_kubectl("get", ["pods"])
        assert (proc.returncode, proc.stdout, proc.stderr) == (1, "  out\n", "err\n")

    @pytest.mark.parametrize("raised, why", _NO_EXIT_STATUS, ids=_NO_EXIT_IDS)
    def test_no_exit_status_is_kubectl_error(self, monkeypatch, raised, why):
        """#3349 / #3539: the environment, named (rc 2, reason kubectl_error)
        — not on PATH and out of time were named here already; no execute
        permission and ENOEXEC escaped as a traceback (`unexpected internal
        error`). A kubectl that answered non-zero is still the caller's own
        fail row."""
        _kubectl(monkeypatch, {"create": raised})
        with pytest.raises(KubectlError, match=why) as exc:
            gi.run_kubectl("create", ["configmap", "x"])
        assert exc.value.reason == "kubectl_error"
        assert "kubectl create configmap x" in str(exc.value)


# ---------------------------------------------------------------------------
# import_dashboard — mutating path
# ---------------------------------------------------------------------------
class TestImportDashboard:
    def test_a_missing_file_is_refused_naming_its_flag(self, tmp_path):
        """#3724: a file of --dashboard-dir gone since the directory was
        judged is a refusal naming the flag and the file (rc 2), read once
        by `_lib_input.read_bytes` — not a per-file "fail" row, and not
        judged again under `--dashboard` (#3276's check named the wrong flag)."""
        from _lib_exitcodes import CallerError
        ghost = tmp_path / "ghost.json"
        with pytest.raises(CallerError) as exc:
            gi.read_dashboards([(str(ghost), "cm", None, "--dashboard-dir")])
        assert str(exc.value) == f"--dashboard-dir {ghost}: {os.strerror(errno.ENOENT)}"

    def test_every_dashboard_is_read_before_any_is_imported(self, tmp_path, monkeypatch):
        """#3724: a --dashboard-dir file gone after the judgement is refused
        before kubectl is run for any of them — not after the ones before it
        were applied to the cluster."""
        from _lib_exitcodes import CallerError
        d = tmp_path / "dd"
        d.mkdir()
        for name in ("a.json", "b.json"):
            (d / name).write_text('{"title": "T"}', encoding="utf-8")
        calls = _kubectl(monkeypatch)
        real = gi._lib_input.selected

        def selected_then_gone(*a, **k):
            chosen = real(*a, **k)
            (d / "b.json").unlink()
            return chosen

        monkeypatch.setattr(gi._lib_input, "selected", selected_then_gone)
        monkeypatch.setattr(sys, "argv", ["grafana_import.py", "--dashboard-dir", str(d)])
        with pytest.raises(CallerError, match="--dashboard-dir .*b.json"):
            gi.main()
        assert calls == []

    def test_kubectl_reads_the_bytes_read_not_the_callers_path(self, tmp_path, monkeypatch):
        """#3724: the dashboard is read once (`_lib_input.read_bytes`);
        kubectl's `--from-file` is a copy of those bytes, never the caller's
        path — kubectl reading it again could see it gone, or other bytes."""
        f = tmp_path / "d.json"
        raw = b'{"title": "T"}'
        f.write_bytes(raw)
        seen = {}

        def create(cmd, **_kw):
            for a in cmd:
                if a.startswith("--from-file="):
                    source = a.split("=", 2)[2]
                    seen["source"], seen["bytes"] = source, Path(source).read_bytes()
            return (0, "kind: ConfigMap", "")

        _kubectl(monkeypatch, {"create": create})
        gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        assert seen["source"] != str(f) and seen["bytes"] == raw, seen

    def test_a_nesting_too_deep_is_invalid_json_not_a_crash(self, tmp_path):
        f = tmp_path / "deep.json"
        f.write_text("[" * 200_000, encoding="utf-8")
        results = gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        assert [r["status"] for r in results] == ["fail"]
        assert results[0]["detail"].startswith("Invalid JSON")

    def test_invalid_json_returns_fail_result(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json", encoding="utf-8")
        results = gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert "Invalid JSON" in results[0]["detail"]

    def test_dry_run_does_not_execute_kubectl(self, tmp_path, monkeypatch, capsys):
        # kubectl should NEVER be started in dry-run.
        def fail_if_called(*args, **kwargs):
            raise AssertionError("subprocess invoked in dry-run mode")
        install_kubectl(monkeypatch, fail_if_called)

        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "Test Dashboard"}), encoding="utf-8")
        results = gi.import_dashboard(str(f), "grafana-dash", "monitoring", dry_run=True,
                                      raw=f.read_bytes())
        statuses = [r["status"] for r in results]
        assert "dry-run" in statuses
        assert all(s != "fail" for s in statuses)

    def test_create_configmap_failure_short_circuits(self, tmp_path, monkeypatch):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")

        calls = _kubectl(monkeypatch, {"create": (1, "", "error: bad name")})
        results = gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        # Only the create failure is recorded; apply and label are skipped.
        assert [c[1] for c in calls] == ["create"]
        assert len(results) == 1
        assert results[0]["action"] == "create_configmap"
        assert results[0]["status"] == "fail"

    def test_happy_path_creates_then_labels(self, tmp_path, monkeypatch):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "Happy"}), encoding="utf-8")

        # create (generate YAML), apply and label all exit 0; apply is
        # handed the manifest create printed.
        applied = {}

        def apply(cmd, **kw):
            applied["stdin"] = kw.get("input")
            return (0, "configmap/cm created\n", "")
        calls = _kubectl(monkeypatch, {"apply": apply})

        results = gi.import_dashboard(str(f), "cm", "monitoring", raw=f.read_bytes())
        assert [c[1] for c in calls] == ["create", "apply", "label"]
        assert applied["stdin"] == b"kind: ConfigMap"
        actions = [r["action"] for r in results]
        statuses = [r["status"] for r in results]
        assert "create_configmap" in actions
        assert "label_configmap" in actions
        assert all(s == "pass" for s in statuses)

    def test_apply_failure_short_circuits(self, tmp_path, monkeypatch):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")

        calls = _kubectl(monkeypatch, {"apply": (1, "", "apply error")})

        results = gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        # Apply fails, label should not be attempted.
        assert [c[1] for c in calls] == ["create", "apply"]
        assert len(results) == 1
        assert results[0]["status"] == "fail"
        assert "apply" in results[0]["detail"].lower()

    # ── #3539: a kubectl that gives no exit status, at each of the three ──

    @pytest.mark.parametrize("raised, why", _NO_EXIT_STATUS, ids=_NO_EXIT_IDS)
    def test_create_without_an_exit_status_ends_the_import(self, tmp_path, monkeypatch,
                                                           raised, why):
        """Nothing is in the cluster yet: KubectlError (rc 2 at run()'s exit),
        and neither apply nor label is started."""
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        calls = _kubectl(monkeypatch, {"create": raised})
        with pytest.raises(KubectlError, match=why):
            gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        assert [c[1] for c in calls] == ["create"]

    @pytest.mark.parametrize("raised, why", _NO_EXIT_STATUS, ids=_NO_EXIT_IDS)
    def test_apply_without_an_exit_status_is_a_caller_error_row(self, tmp_path, monkeypatch,
                                                                raised, why):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        calls = _kubectl(monkeypatch, {"apply": raised})
        results = gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        assert [c[1] for c in calls] == ["create", "apply"]      # no label
        assert [(r["action"], r["status"], r.get("caller_error")) for r in results] == [
            ("create_configmap", "fail", True)]
        assert why in results[0]["detail"] and "kubectl apply" in results[0]["detail"]

    @pytest.mark.parametrize("raised, why", _NO_EXIT_STATUS, ids=_NO_EXIT_IDS)
    def test_label_without_an_exit_status_after_the_apply_is_never_a_pass(
            self, tmp_path, monkeypatch, raised, why):
        """Part-way: the ConfigMap is applied, the label that makes Grafana's
        sidecar load it is not. The report keeps the applied row and adds a
        caller-error row that says so — it was a traceback for EACCES /
        ENOEXEC, and one ERROR line that dropped the applied row for the
        other two."""
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        _kubectl(monkeypatch, {"label": raised})
        results = gi.import_dashboard(str(f), "cm", "ns", raw=f.read_bytes())
        assert [(r["action"], r["status"], r.get("caller_error")) for r in results] == [
            ("create_configmap", "pass", None), ("label_configmap", "fail", True)]
        assert "its label is NOT confirmed" in results[1]["detail"] and why in results[1]["detail"]

    @pytest.mark.parametrize("flags", [[], ["--json"]], ids=["text", "json"])
    def test_the_cli_exits_2_and_says_fail_when_the_label_cannot_be_started(
            self, tmp_path, monkeypatch, capsys, flags):
        import _lib_exitcodes
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        _kubectl(monkeypatch, {"label": PermissionError(errno.EACCES, "Permission denied")})
        monkeypatch.setattr(sys, "argv", ["grafana_import.py", "--dashboard", str(f), *flags])
        try:
            rc = _lib_exitcodes.emit_result(gi.main())
        except SystemExit as exc:
            rc = exc.code
        out = capsys.readouterr().out
        assert rc == 2
        if flags:
            doc = json.loads(out)
            assert doc["status"] == "fail"
            assert [a["status"] for a in doc["actions"]] == ["pass", "fail"]
        else:
            assert "Overall: FAIL" in out and "Overall: PASS" not in out
            assert "its label is NOT confirmed" in out


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
    install_kubectl(monkeypatch, fake)
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
            _as_run(gi.main())
        # parser.error() exits 2.
        assert exc.value.code == 2

    def test_verify_pass_exits_zero(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "x", "status": "pass", "detail": "ok"}])
        cli_argv("grafana_import.py", "--verify", "--namespace", "monitoring")
        with pytest.raises(SystemExit) as exc:
            _as_run(gi.main())
        assert exc.value.code == 0

    @pytest.mark.parametrize("extra", [(), ("--json",)])
    def test_verify_fail_exits_one(self, monkeypatch, cli_argv, extra):
        """Under --json too: the document's status is the exit (#3216)."""
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "x", "status": "fail", "detail": "broken"}])
        cli_argv("grafana_import.py", "--verify", *extra)
        with pytest.raises(SystemExit) as exc:
            _as_run(gi.main())
        assert exc.value.code == 1

    @pytest.mark.parametrize("extra", [(), ("--json",)])
    def test_missing_dashboard_file_exits_caller_error(self, tmp_path, capsys, cli_argv, extra):
        """#452: a dashboard file that is not there is the caller's — rc 2,
        under --json too. #3617: nothing was answered, so no document —
        stdout empty, the missing file named on stderr (it used to be a
        document saying `fail`, #3216)."""
        cli_argv("grafana_import.py", "--dashboard", str(tmp_path / "nope.json"),
                 "--dry-run", *extra)
        with pytest.raises(SystemExit) as exc:
            _run_main(gi.main)
        assert exc.value.code == 2
        out = capsys.readouterr()
        if extra:
            assert out.out == ""
            assert "nope.json: No such file or directory" in out.err, out.err

    def test_verify_json_output(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gi, "verify_dashboards",
                            lambda ns: [{"check": "c", "status": "pass", "detail": "d"}])
        cli_argv("grafana_import.py", "--verify", "--json")
        with pytest.raises(SystemExit):
            _as_run(gi.main())
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

    def test_an_empty_dashboard_dir_is_named_not_a_usage_error(self, cli_argv):
        """#3693: `--dashboard-dir ""` (an unset variable) is judged by its
        declaration and named — it fell through to "Provide --dashboard,
        --dashboard-dir, or --verify"."""
        cli_argv("grafana_import.py", "--dashboard-dir", "")
        with pytest.raises(CallerError) as exc:
            gi.main()
        assert exc.value.reason == "dir_not_found"
        assert str(exc.value).startswith("--dashboard-dir '': "), str(exc.value)

    def test_dashboard_dir_no_jsons_exits_caller_error(self, monkeypatch, tmp_path, capsys, cli_argv):
        d = tmp_path / "empty-dir"
        d.mkdir()
        (d / "readme.txt").write_text("x", encoding="utf-8")  # not .json
        cli_argv("grafana_import.py", "--dashboard-dir", str(d))
        # #452: empty dir = caller error (2), consistent with dir-not-found;
        # #3693: named by the declaration (flag, path, what it looks for).
        with pytest.raises(CallerError) as exc:
            gi.main()
        assert exc.value.reason == "input_empty"
        assert str(exc.value) == f"--dashboard-dir {d}: selects nothing (*.json)"

    def test_single_dashboard_dry_run_exits_zero(self, monkeypatch, tmp_path, capsys, cli_argv):
        f = tmp_path / "dash.json"
        f.write_text(json.dumps({"title": "T"}), encoding="utf-8")
        cli_argv("grafana_import.py", "--dashboard", str(f), "--dry-run")
        with pytest.raises(SystemExit) as exc:
            _as_run(gi.main())
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
    """`--verify --json` when kubectl gives no answer: rc 2, the reason on
    stderr, no traceback — not a verify report, and (#3558) no envelope."""

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
        assert r.stdout == "", r.stdout  # #3558: a caller error carries no document
        assert "Forbidden" in r.stderr, r.stderr

    def test_no_kubectl_on_path(self, tmp_path):
        empty = tmp_path / "no-kubectl"
        empty.mkdir()
        r = _verify_cli(tmp_path, str(empty))
        assert r.returncode == EXIT_CALLER_ERROR, r.stdout + r.stderr
        assert "Traceback" not in r.stderr
        assert r.stdout == "", r.stdout  # #3558: a caller error carries no document
        assert "kubectl is not available" in r.stderr, r.stderr


def test_json_document_is_utf8_not_ascii_escaped(tmp_path):
    """#3216: the document is run()'s to write — UTF-8 text, as every
    --json document; this tool's own `json.dumps` escaped a non-ASCII
    dashboard file name to `\\uXXXX`."""
    dash = tmp_path / "儀表板.json"
    dash.write_text('{"title": "t", "panels": []}', encoding="utf-8")
    p = subprocess.run(
        [sys.executable, os.path.join(_TOOLS_DIR, "grafana_import.py"),
         "--dashboard", str(dash), "--dry-run", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    assert "from 儀表板.json" in p.stdout, p.stdout
    assert json.loads(p.stdout)["status"] == "pass"
