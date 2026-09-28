"""#2219: Alertmanager's own parser (`amtool check-config`) as the last gate
of generate_alertmanager_routes.py.

The schema and the generator's Python checks accept values Alertmanager
refuses at load time (a webhook URL `http://[1]/`; a host carrying U+0085 that
the YAML emitter folds into a line break). Before this, the generator wrote
such a config at rc 0, and `--apply` pushed it to the cluster; a failed
`/-/reload` was only a WARN, still rc 0.

The contract pinned here:

* amtool on PATH, accepts  → output written, rc unchanged, stderr says so;
* amtool on PATH, rejects  → rc 1, amtool's message on stderr, NOTHING written
  (not to -o, not to stdout) and, under --apply, kubectl never asked to apply;
* amtool not runnable      → rc 2 (environment), nothing written;
* amtool absent            → rc and output unchanged, but a NOTICE says the
  config was NOT validated — never silent;
* fragment (render) mode   → always a NOTICE: a fragment has no root receiver,
  so amtool cannot check it at all;
* --apply reload failure   → rc 2;
* --validate (#2260)       → the same verdicts, on the config assembled on the
  BUILT-IN base; without amtool a NOTICE naming that base, rc unchanged; an
  invariant ``assemble_configmap`` refuses is FAIL rc 1, not a traceback.

The CLI-level tests use a fake `amtool` shell stub on PATH. ⚠️ An
extensionless shell stub is not executable on Windows (see the cluster-axis
note in test_generate_routes_orchestration.py), so those are skipped there;
the in-process tests cover the same decisions on every platform.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from factories import make_routing_config, make_tenant_yaml

import _grar_render as render
import generate_alertmanager_routes as gar

_GAR = (Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
        / "generate_alertmanager_routes.py")

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="shell-stub amtool is not executable on Windows")

_NOT_VALIDATED = ("NOTICE: amtool not found on PATH; generated Alertmanager "
                  "config was NOT validated by Alertmanager")


@pytest.fixture
def tenant_dir(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "t1.yaml").write_text(
        make_tenant_yaml("t1", keys={"mysql_connections": "70"},
                         routing=make_routing_config("t1")),
        encoding="utf-8")
    return d


def _install_amtool(tmp_path, rc: int, message: str) -> tuple[Path, Path]:
    """A fake `amtool` that copies the file it was handed to *seen* (so the
    test can compare it with what was written), prints *message*, exits *rc*.
    Returns (bin_dir, seen)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "amtool-saw.yml"
    stub = bin_dir / "amtool"
    stub.write_text(
        "#!/bin/sh\n"
        # the tool runs with PATH = this dir only, so name cp's home here
        "PATH=/usr/bin:/bin\n"
        f"cp \"$2\" '{seen}'\n"
        f"echo \"Checking '$2'  {message}\"\n"
        f"exit {rc}\n",
        encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir, seen


def _gar(argv, path: str):
    env = dict(os.environ, PATH=path)
    return subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                          capture_output=True, timeout=180, env=env)


def _empty_path(tmp_path) -> str:
    d = tmp_path / "empty-path"
    d.mkdir()
    return str(d)


# ── --output-configmap ──────────────────────────────────────────────
@_POSIX_ONLY
class TestOutputConfigmapGate:

    def test_accepted_writes_and_amtool_saw_the_written_text(self, tenant_dir, tmp_path):
        """amtool must have checked the alertmanager.yml INSIDE the file that
        was written — not a re-render of it."""
        bin_dir, seen = _install_amtool(tmp_path, 0, "SUCCESS")
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], str(bin_dir))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert out.is_file()
        assert "accepted by Alertmanager's parser" in err, err
        assert _NOT_VALIDATED not in err
        written_am = yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"]
        assert seen.read_bytes() == written_am.encode("utf-8")

    def test_rejected_writes_nothing_and_exits_1(self, tenant_dir, tmp_path):
        bin_dir, seen = _install_amtool(tmp_path, 1, "FAILED: fake-rejection-2219")
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], str(bin_dir))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert not out.exists(), "a config amtool rejected was written anyway"
        assert "fake-rejection-2219" in err, err
        assert "nothing was written" in err, err
        assert seen.is_file(), "amtool was never called — the rc 1 came from elsewhere"

    def test_rejected_prints_nothing_to_stdout_either(self, tenant_dir, tmp_path):
        """Without -o the ConfigMap goes to stdout — that is an emission too."""
        bin_dir, _seen = _install_amtool(tmp_path, 1, "FAILED: fake-rejection-2219")
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap"],
                 str(bin_dir))
        assert r.returncode == 1, r.stderr.decode("utf-8", "replace")
        assert b"apiVersion" not in r.stdout
        assert b"alertmanager.yml" not in r.stdout

    def test_unrunnable_amtool_is_a_caller_error(self, tenant_dir, tmp_path):
        """rc 127 is what `_run_binary` reports for "could not run" — an
        environment failure (2), not a config verdict (1)."""
        bin_dir, _seen = _install_amtool(tmp_path, 127, "not really amtool")
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], str(bin_dir))
        assert r.returncode == 2, r.stderr.decode("utf-8", "replace")
        assert not out.exists()

    def test_crashing_amtool_is_a_caller_error_not_a_rejection(self, tenant_dir, tmp_path):
        """A Go panic exits 2 with no `FAILED:` verdict: the validator broke,
        the config was never judged — environment (2), not config wrong (1)."""
        bin_dir, _seen = _install_amtool(tmp_path, 2, "panic: runtime error")
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], str(bin_dir))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 2, err
        assert not out.exists()
        assert "could not validate" in err, err

    def test_no_amtool_says_not_validated_and_output_is_unchanged(self, tenant_dir, tmp_path):
        """rc and bytes identical to a validated run; only the NOTICE differs."""
        bin_dir, _seen = _install_amtool(tmp_path, 0, "SUCCESS")
        validated = tmp_path / "validated.yaml"
        unvalidated = tmp_path / "unvalidated.yaml"
        r1 = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                   "-o", str(validated)], str(bin_dir))
        r2 = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                   "-o", str(unvalidated)], _empty_path(tmp_path))
        err2 = r2.stderr.decode("utf-8", "replace")
        assert r1.returncode == 0 and r2.returncode == 0, err2
        assert _NOT_VALIDATED in err2, err2
        assert "accepted by Alertmanager's parser" not in err2
        assert validated.read_bytes() == unvalidated.read_bytes()


# ── render (fragment) mode ──────────────────────────────────────────
@_POSIX_ONLY
class TestFragmentIsNeverClaimedValidated:

    @pytest.mark.parametrize("with_amtool", [True, False])
    def test_fragment_says_not_validated(self, tenant_dir, tmp_path, with_amtool):
        if with_amtool:
            bin_dir, seen = _install_amtool(tmp_path, 0, "SUCCESS")
            path = str(bin_dir)
        else:
            seen, path = None, _empty_path(tmp_path)
        out = tmp_path / "frag.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "-o", str(out)], path)
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert out.is_file()
        assert "routing fragment mode; generated output was NOT validated" in err, err
        if seen is not None:
            assert not seen.exists(), "amtool cannot validate a fragment; it must not be run"


# ── --apply (in-process: no shell stub, runs on every platform) ─────
def _existing_cm_json() -> str:
    import json
    return json.dumps({"data": {"alertmanager.yml": yaml.dump({
        "route": {"receiver": "default", "routes": []},
        "receivers": [{"name": "default"}],
        "inhibit_rules": [],
    })}})


def _fake_cluster(monkeypatch, *, amtool_rc: int | None, curl_rc: int = 0):
    """Patch `_run_binary` + amtool discovery; return the argv log.
    amtool_rc None = amtool not on PATH."""
    calls: list[list[str]] = []

    def fake(argv, *, timeout, stdin_text=None):
        calls.append(list(argv))
        if argv[:2] == ["kubectl", "get"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_existing_cm_json(), stderr="")
        if argv[0] == "/fake/amtool":
            return subprocess.CompletedProcess(argv, amtool_rc, stdout="Checking x  FAILED: nope\n"
                                               if amtool_rc else "SUCCESS\n", stderr="")
        if argv[0] == "curl":
            return subprocess.CompletedProcess(argv, curl_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    monkeypatch.setattr(render.shutil, "which",
                        lambda name: "/fake/amtool" if (name == "amtool" and amtool_rc is not None)
                        else None)
    return calls


_ROUTES = [{"receiver": "tenant-t1", "matchers": ['tenant="t1"']}]
_RECEIVERS = [{"name": "tenant-t1", "webhook_configs": [{"url": "https://x.example.com/h"}]}]


class TestApplyGate:

    def _run_apply_mode(self):
        with pytest.raises(SystemExit) as exc:
            gar._apply_mode(_ROUTES, _RECEIVERS, [], "monitoring",
                            "alertmanager-config", yes_flag=True)
        return exc.value.code

    def test_rejected_config_never_reaches_kubectl_apply(self, monkeypatch, capsys):
        calls = _fake_cluster(monkeypatch, amtool_rc=1)
        assert self._run_apply_mode() == 1
        kubectl_writes = [c for c in calls if c[:2] in (["kubectl", "apply"],
                                                         ["kubectl", "create"])]
        assert kubectl_writes == [], calls
        assert not any(c[0] == "curl" for c in calls), calls
        assert any(c[0] == "/fake/amtool" for c in calls), "the gate never ran"
        assert "nothing was applied" in capsys.readouterr().err

    def test_amtool_saw_the_exact_text_passed_to_kubectl(self, monkeypatch):
        calls = _fake_cluster(monkeypatch, amtool_rc=0)
        seen: list[str] = []
        real_fake = render._run_binary

        def spy(argv, *, timeout, stdin_text=None):
            if argv[0] == "/fake/amtool":
                seen.append(Path(argv[2]).read_text(encoding="utf-8"))
            return real_fake(argv, timeout=timeout, stdin_text=stdin_text)

        monkeypatch.setattr(render, "_run_binary", spy)
        assert self._run_apply_mode() == 0
        create = next(c for c in calls if c[:2] == ["kubectl", "create"])
        literal = next(a for a in create if a.startswith("--from-literal=alertmanager.yml="))
        assert seen == [literal[len("--from-literal=alertmanager.yml="):]]

    def test_unrunnable_amtool_is_exit_2_and_applies_nothing(self, monkeypatch):
        calls = _fake_cluster(monkeypatch, amtool_rc=127)
        assert self._run_apply_mode() == 2
        assert not any(c[:2] == ["kubectl", "apply"] for c in calls), calls

    def test_no_amtool_applies_with_notice(self, monkeypatch, capsys):
        calls = _fake_cluster(monkeypatch, amtool_rc=None)
        assert self._run_apply_mode() == 0
        assert any(c[:2] == ["kubectl", "apply"] for c in calls), calls
        assert _NOT_VALIDATED in capsys.readouterr().err

    def test_reload_failure_is_exit_2(self, monkeypatch, capsys):
        calls = _fake_cluster(monkeypatch, amtool_rc=0, curl_rc=22)
        assert self._run_apply_mode() == 2
        assert any(c[0] == "curl" for c in calls), "exit 2 came from before the reload"
        assert "ERROR: Alertmanager reload failed" in capsys.readouterr().err

    def test_control_reload_success_is_exit_0(self, monkeypatch):
        """Without this, the row above passes for a tool that always exits 2."""
        _fake_cluster(monkeypatch, amtool_rc=0, curl_rc=0)
        assert self._run_apply_mode() == 0


# ── #2260: --validate goes through the same gate ────────────────────
# Before this, --validate printed OK at rc 0 for a tree that --output-configmap
# then refused (`http://[1]/`). It now assembles the config on the BUILT-IN
# base (`--validate --base-config` stays rc 2, #1616) and hands it to
# `amtool_gate` before printing OK — same verdicts as the write paths.
_VALIDATE_NOT_VALIDATED = gar.VALIDATE_AMTOOL_NOT_FOUND_NOTICE


@_POSIX_ONLY
class TestValidateGate:

    def test_rejected_is_exit_1_and_never_says_ok(self, tenant_dir, tmp_path):
        bin_dir, seen = _install_amtool(tmp_path, 1, "FAILED: fake-rejection-2260")
        r = _gar(["--config-dir", str(tenant_dir), "--validate"], str(bin_dir))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert b"OK: all configs valid" not in r.stdout
        assert "fake-rejection-2260" in err, err
        assert seen.is_file(), "amtool was never called — the rc 1 came from elsewhere"

    def test_amtool_saw_a_complete_config_on_the_builtin_base(self, tenant_dir, tmp_path):
        """A complete config (root receiver from the built-in base) — not a
        fragment, which amtool refuses for that alone."""
        bin_dir, seen = _install_amtool(tmp_path, 0, "SUCCESS")
        r = _gar(["--config-dir", str(tenant_dir), "--validate"], str(bin_dir))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert b"OK: all configs valid" in r.stdout
        assert "accepted by Alertmanager's parser" in err, err
        am = yaml.safe_load(seen.read_text(encoding="utf-8"))
        assert am["route"]["receiver"] == render._DEFAULT_BASE_CONFIG["route"]["receiver"]
        assert "tenant-t1" in [x["name"] for x in am["receivers"]]

    @pytest.mark.parametrize("rc,message", [(127, "not really amtool"),
                                            (2, "panic: runtime error")],
                             ids=["unrunnable", "crash"])
    def test_amtool_own_failure_is_exit_2(self, tenant_dir, tmp_path, rc, message):
        """No verdict on the config → environment (2), never a rejection."""
        bin_dir, _seen = _install_amtool(tmp_path, rc, message)
        r = _gar(["--config-dir", str(tenant_dir), "--validate"], str(bin_dir))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 2, err
        assert b"OK: all configs valid" not in r.stdout
        assert "could not validate" in err, err

    def test_no_amtool_keeps_rc_0_and_says_builtin_base(self, tenant_dir, tmp_path):
        r = _gar(["--config-dir", str(tenant_dir), "--validate"],
                 _empty_path(tmp_path))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert b"OK: all configs valid" in r.stdout
        assert _VALIDATE_NOT_VALIDATED in err, err
        assert "built-in default base" in _VALIDATE_NOT_VALIDATED

    def test_validate_with_base_config_is_still_a_caller_error(self, tenant_dir, tmp_path):
        """#1616: --validate never reads --base-config; #2260 does not change that."""
        base = tmp_path / "base.yml"
        base.write_text("route:\n  receiver: default\nreceivers:\n  - name: default\n",
                        encoding="utf-8")
        r = _gar(["--config-dir", str(tenant_dir), "--validate",
                  "--base-config", str(base)], _empty_path(tmp_path))
        assert r.returncode == 2, r.stderr.decode("utf-8", "replace")


class TestAssemblyValueErrorIsAVerdict:
    """An invariant `assemble_configmap` refuses (ValueError) is a CONFIG
    verdict: FAIL at rc 1, not a traceback."""

    def test_validate_mode(self, monkeypatch, capsys):
        def boom(*_a, **_k):
            raise ValueError("fake invariant #2260")
        monkeypatch.setattr(gar, "assemble_configmap", boom)
        with pytest.raises(SystemExit) as exc:
            gar._validate_mode(_ROUTES, _RECEIVERS, [], [])
        assert exc.value.code == 1
        cap = capsys.readouterr()
        assert "FAIL:" in cap.err and "fake invariant #2260" in cap.err, cap.err
        assert "OK: all configs valid" not in cap.out

    @_POSIX_ONLY
    def test_output_configmap_with_a_base_that_breaks_an_invariant(self, tenant_dir, tmp_path):
        """A base whose Silent-Mode inhibit could mute a platform alert (the
        try-local shape): measured as a traceback at rc 1 before."""
        base = tmp_path / "base.yml"
        base.write_text(yaml.safe_dump({
            "route": {"receiver": "default"},
            "receivers": [{"name": "default"}],
            "inhibit_rules": [{
                "source_matchers": ['alertname = "TenantSilentWarning"', 'tenant =~ ".+"'],
                "target_matchers": ['severity = "warning"', 'tenant =~ ".+"'],
                "equal": ["tenant"]}],
        }), encoding="utf-8")
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "--base-config", str(base), "-o", str(out)], _empty_path(tmp_path))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert "Traceback" not in err, err
        assert "FAIL:" in err and "invariant violated" in err, err
        assert not out.exists()


# ── the real amtool, when one is on PATH (skipped otherwise) ────────
_REAL_AMTOOL = shutil.which("amtool")
_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(_REAL_AMTOOL is None,
                    reason="amtool not on PATH — the real-parser rows need it")
class TestValidateWithRealAmtool:

    def _run(self, conf_dir):
        return _gar(["--config-dir", str(conf_dir), "--validate"],
                    os.environ.get("PATH", ""))

    def test_url_alertmanager_refuses_is_exit_1(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "t1.yaml").write_text(
            make_tenant_yaml("t1", routing={"receiver": {
                "type": "webhook", "url": "http://[1]/"}}),
            encoding="utf-8")
        r = self._run(d)
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert "FAILED:" in err and "http://[1]/" in err, err
        assert b"OK: all configs valid" not in r.stdout

    def test_repo_conf_d_is_accepted(self):
        r = self._run(_REPO / "components" / "threshold-exporter" / "config" / "conf.d")
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert "accepted by Alertmanager's parser" in err, err
