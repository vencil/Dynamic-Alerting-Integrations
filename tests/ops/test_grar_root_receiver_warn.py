"""#2660: one WARN when the Alertmanager config about to be deployed has a root
route whose receiver has no integration.

Every alert no child route claims ends at the root receiver — the platform's
own self-monitoring alerts included, unless conf.d routes them. The built-in
base's ``default`` (and the shipped k8s/03-monitoring one) is name-only, so
those alerts reach no one. The owner's decision (option A): a WARN, only in the
two modes that emit / apply a COMPLETE config (``--output-configmap``,
``--apply``); never an error, never escalated by ``--strict``, rc unchanged.

Also pins the shared predicate ``receiver_integration_kinds``: an empty
``*_configs`` list is not an integration, for the WARN and for
``explain_route``'s ``receiver_type`` alike.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from factories import make_routing_config, make_tenant_yaml

import _grar_render as render
import explain_route as er
import generate_alertmanager_routes as gar

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")

_WARN = "has no integration (no non-empty *_configs)"
_DOC = "byo-alertmanager-integration.en.md §11"


@pytest.fixture
def tenant_dir(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(
        make_tenant_yaml("t1", keys={"mysql_connections": "70"},
                         routing=make_routing_config("t1")),
        encoding="utf-8")
    return d


def _gar(argv, tmp_path) -> tuple[int, str]:
    """Run the CLI with amtool absent from PATH (the gate then only NOTICEs)."""
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    r = subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                       capture_output=True, timeout=180,
                       env=dict(os.environ, PATH=str(empty)))
    return r.returncode, r.stderr.decode("utf-8", "replace")


def _base(tmp_path, root_receiver: dict) -> Path:
    p = tmp_path / "base.yaml"
    p.write_text(yaml.safe_dump({
        "route": {"receiver": root_receiver["name"],
                  "group_by": ["alertname"]},
        "receivers": [root_receiver],
    }), encoding="utf-8")
    return p


# ── the shared predicate ────────────────────────────────────────────
class TestReceiverIntegrationKinds:

    @pytest.mark.parametrize("receiver", [
        {"name": "x"},
        {"name": "x", "webhook_configs": []},
        {"name": "x", "webhook_configs": [], "email_configs": None},
        "not-a-mapping",
    ])
    def test_no_integration(self, receiver):
        assert render.receiver_integration_kinds(receiver) == []

    def test_only_non_empty_lists_count(self):
        assert render.receiver_integration_kinds({
            "name": "x", "webhook_configs": [{"url": "https://a.example/h"}],
            "email_configs": []}) == ["webhook"]

    def test_explain_route_uses_it(self):
        receivers = {
            "bare": {"name": "bare"},
            "empty": {"name": "empty", "webhook_configs": []},
            "two": {"name": "two",
                    "webhook_configs": [{"url": "https://a.example/h"}],
                    "slack_configs": [{"channel": "#x"}]},
        }
        assert er._receiver_type("bare", receivers, {}) == "none"
        assert er._receiver_type("empty", receivers, {}) == "none"
        assert er._receiver_type("two", receivers, {}) == "webhook+slack"


# ── --output-configmap ──────────────────────────────────────────────
class TestOutputConfigmap:

    def test_builtin_base_warns_rc_0(self, tenant_dir, tmp_path):
        rc, err = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                        "-o", str(tmp_path / "cm.yaml")], tmp_path)
        assert rc == 0, err
        assert err.count(_WARN) == 1, err
        assert "'default'" in err and _DOC in err, err

    def test_strict_does_not_escalate(self, tenant_dir, tmp_path):
        rc, err = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                        "--strict", "-o", str(tmp_path / "cm.yaml")], tmp_path)
        assert rc == 0, err
        assert _WARN in err, err

    def test_base_with_integration_no_warn(self, tenant_dir, tmp_path):
        base = _base(tmp_path, {"name": "noc", "webhook_configs": [
            {"url": "https://noc.example.com/h"}]})
        rc, err = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                        "--base-config", str(base),
                        "-o", str(tmp_path / "cm.yaml")], tmp_path)
        assert rc == 0, err
        assert _WARN not in err, err

    def test_base_with_empty_configs_list_warns(self, tenant_dir, tmp_path):
        base = _base(tmp_path, {"name": "noc", "webhook_configs": []})
        rc, err = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                        "--base-config", str(base),
                        "-o", str(tmp_path / "cm.yaml")], tmp_path)
        assert rc == 0, err
        assert _WARN in err and "'noc'" in err, err


# ── modes that do not deploy a complete config ──────────────────────
class TestOtherModesDoNotWarn:

    @pytest.mark.parametrize("extra", [[], ["--dry-run"], ["--validate"]])
    def test_no_warn(self, tenant_dir, tmp_path, extra):
        argv = ["--config-dir", str(tenant_dir), *extra]
        if not extra:
            argv += ["-o", str(tmp_path / "frag.yaml")]
        rc, err = _gar(argv, tmp_path)
        assert rc == 0, err
        assert _WARN not in err, err


# ── --apply (in-process fake cluster) ───────────────────────────────
def _fake_cluster(monkeypatch, root_receiver: dict) -> list[list[str]]:
    calls: list[list[str]] = []
    cm = json.dumps({"data": {"alertmanager.yml": yaml.dump({
        "route": {"receiver": root_receiver["name"], "routes": []},
        "receivers": [root_receiver],
        "inhibit_rules": [],
    })}})

    def fake(argv, *, timeout, stdin_text=None):
        calls.append(list(argv))
        if argv[:2] == ["kubectl", "get"]:
            return subprocess.CompletedProcess(argv, 0, stdout=cm, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    return calls


_ROUTES = [{"receiver": "tenant-t1", "matchers": ['tenant="t1"']}]
_RECEIVERS = [{"name": "tenant-t1",
               "webhook_configs": [{"url": "https://x.example.com/h"}]}]


class TestApply:

    def _run(self, strict=False) -> int:
        with pytest.raises(SystemExit) as exc:
            gar._apply_mode(_ROUTES, _RECEIVERS, [], "monitoring",
                            "alertmanager-config", yes_flag=True, strict=strict)
        return exc.value.code

    @pytest.mark.parametrize("strict", [False, True])
    def test_empty_cluster_root_receiver_warns(self, monkeypatch, capsys, strict):
        calls = _fake_cluster(monkeypatch, {"name": "default"})
        assert self._run(strict) == 0
        assert any(c[:2] == ["kubectl", "apply"] for c in calls), calls
        assert capsys.readouterr().err.count(_WARN) == 1

    def test_cluster_root_receiver_with_integration_no_warn(self, monkeypatch, capsys):
        _fake_cluster(monkeypatch, {"name": "noc", "pagerduty_configs": [
            {"routing_key": "k"}]})
        assert self._run() == 0
        assert _WARN not in capsys.readouterr().err
