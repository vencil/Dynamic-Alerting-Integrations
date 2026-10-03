"""#2506: ``--apply`` against a cluster ConfigMap whose kept inhibit rules
break a platform invariant ends in a ``FAIL:`` verdict at rc 1, not a
traceback — the same shape #2260 gave ``--output-configmap`` / ``--validate``.

The merge keeps the cluster's own inhibit rules, so an existing rule that
would suppress the always-firing Watchdog heartbeat (ADR-025) only surfaces
there. Before the fix ``_apply_mode`` caught ``AlertmanagerConfigRejected``
alone and the merge's ``ValueError`` escaped.

kubectl is faked in-process (``_grar_render._run_binary``), as the other
``--apply`` tests do: a PATH stub is not executable on Windows and would
silently fall through to a real binary.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys

import pytest
import yaml

from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION

_WATCHDOG_RULE = {"source_matchers": ['severity="critical"'],
                  "target_matchers": ['alertname="Watchdog"']}
_BENIGN_RULE = {"source_matchers": ['severity="critical"'],
                "target_matchers": ['severity="info"']}


@pytest.fixture
def tenant_dir(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(
        "tenants:\n  t1:\n    _routing:\n      receiver:\n"
        "        type: webhook\n        url: https://hooks.example.com/alert\n",
        encoding="utf-8")
    return d


def _run_apply(monkeypatch, tmp_path, config_dir, inhibit_rule):
    """``generate-routes --apply --yes`` in-process against a fake cluster
    whose ``alertmanager.yml`` carries *inhibit_rule*. Returns (rc, calls)."""
    render = importlib.import_module("_grar_render")
    gar = importlib.import_module("generate_alertmanager_routes")
    existing = {"route": {"receiver": "default"},
                "receivers": [{"name": "default"}],
                "inhibit_rules": [inhibit_rule]}
    calls: list[list[str]] = []

    def fake(argv, *, timeout, stdin_text=None, text=True):
        calls.append(list(argv))
        out = (json.dumps({"data": {"alertmanager.yml": yaml.safe_dump(existing)}})
               if argv[:2] == ["kubectl", "get"] else "")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    monkeypatch.setenv("PATH", str(tmp_path))          # no amtool on PATH
    monkeypatch.setattr(sys, "argv", [
        "generate_alertmanager_routes.py", "--config-dir", str(config_dir),
        "--apply", "--yes"])
    # Only SystemExit may leave main(): any other exception IS the traceback.
    with pytest.raises(SystemExit) as exc:
        gar.main()
    return exc.value.code, calls


def test_cluster_rule_suppressing_watchdog_is_a_fail_verdict(
        monkeypatch, capsys, tmp_path, tenant_dir):
    rc, calls = _run_apply(monkeypatch, tmp_path, tenant_dir, _WATCHDOG_RULE)
    out = capsys.readouterr()
    assert rc == EXIT_VIOLATION, out.err
    assert "FAIL: the assembled Alertmanager config was refused" in out.err
    assert "nothing was applied to the cluster" in out.err
    assert "Watchdog" in out.err
    assert "Traceback" not in out.err + out.out
    # Read the cluster, refused before amtool / kubectl apply / reload.
    assert [c[:2] for c in calls] == [["kubectl", "get"]], calls


def test_control_a_benign_cluster_rule_is_applied(
        monkeypatch, capsys, tmp_path, tenant_dir):
    """Without it, the test above passes against a fake that never lets
    any --apply reach `kubectl apply`."""
    rc, calls = _run_apply(monkeypatch, tmp_path, tenant_dir, _BENIGN_RULE)
    out = capsys.readouterr()
    assert rc == EXIT_OK, out.err
    assert "FAIL:" not in out.err
    assert ["kubectl", "apply"] in [c[:2] for c in calls], calls


def test_only_the_merge_step_is_converted(monkeypatch):
    """A `ValueError` from outside the merge is not dressed up as a config
    verdict: `apply_to_configmap` wraps the merge step alone."""
    render = importlib.import_module("_grar_render")

    def boom(*_a, **_k):
        raise ValueError("not an invariant")

    monkeypatch.setattr(render, "_read_existing_configmap", boom)
    with pytest.raises(ValueError) as exc:
        render.apply_to_configmap([], [], [], "monitoring", "cm")
    assert not isinstance(exc.value, render.AlertmanagerConfigInvariantViolated)
