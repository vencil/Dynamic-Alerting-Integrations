"""#2219 / #3689 / #3793: Alertmanager's own config loader (da-read) as the
last gate of generate_alertmanager_routes.py — the ONE gate every mode shares
(`_grar_render.alertmanager_check`).

The schema and the generator's Python checks accept values Alertmanager
refuses at load time (a webhook URL `http://[1]/`; a host carrying U+0085 that
the YAML emitter folds into a line break). Before #2219, the generator wrote
such a config at rc 0, and `--apply` pushed it to the cluster; a failed
`/-/reload` was only a WARN, still rc 0.

The contract pinned here:

* the loader accepts → output written, rc 0, stderr says so, and what it read
  is byte for byte the text that was written (not a re-render);
* the loader refuses → rc 1, its reason on stderr, NOTHING written (not to -o,
  not to stdout) and, under --apply, kubectl never asked to apply;
* no reading could be had (no da-read, it crashed) → rc 2 (environment),
  nothing written;
* whether an `amtool` is on PATH changes nothing (#3793);
* --apply reload failure → rc 2;
* --validate (#2260) → the same verdicts, on the config assembled on the
  BUILT-IN base; an invariant ``assemble_configmap`` refuses is FAIL rc 1,
  not a traceback.

The CLI-level tests put a shell wrapper in front of the real da-read
(``$DA_READ_BINARY``) to see what it was handed, or a shell stub in its place.
⚠️ An extensionless shell stub is not executable on Windows (see the
cluster-axis note in test_generate_routes_orchestration.py), so those are
skipped there; the in-process tests cover the same decisions on every
platform.
"""
from __future__ import annotations

import json
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
    reason="a shell-stub da-read is not executable on Windows")

_ACCEPTED = "Alertmanager's config loader (da-read) accepted"
_REFUSED = "Alertmanager's config loader refuses"


def _tree(d: Path, routing: dict) -> Path:
    d.mkdir()
    # A root platform file declaring the tenant's threshold: without one the
    # key is undecidable, which fails --validate before Alertmanager is asked
    # (#3016).
    (d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    (d / "t1.yaml").write_text(
        make_tenant_yaml("t1", keys={"mysql_connections": "70"}, routing=routing),
        encoding="utf-8")
    return d


@pytest.fixture
def tenant_dir(tmp_path):
    return _tree(tmp_path / "conf.d", make_routing_config("t1"))


@pytest.fixture
def refused_dir(tmp_path):
    """A webhook URL the generator passes and Alertmanager's loader refuses."""
    return _tree(tmp_path / "conf.d-refused",
                 {"receiver": {"type": "webhook", "url": "http://[1]/"}})


def _real_da_read() -> str:
    real = os.environ.get("DA_READ_BINARY", "").strip() or shutil.which("da-read")
    if not real:
        pytest.fail("no da-read ($DA_READ_BINARY / PATH): tests/conftest.py builds "
                    "one when `go` is on PATH")
    return real


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _spying_da_read(tmp_path) -> tuple[Path, Path]:
    """A wrapper in front of the real da-read that copies what `da-read
    alertmanager` is handed on stdin to *seen*, then lets the real one read
    the same bytes. Returns (wrapper, seen)."""
    seen = tmp_path / "da-read-saw.yml"
    cat = shutil.which("cat") or "/bin/cat"
    real = _real_da_read()
    wrapper = _executable(tmp_path / "da-read-spy", (
        "#!/bin/sh\n"
        'if [ "$1" = alertmanager ]; then\n'
        f"  '{cat}' > '{seen}'\n"
        f"  exec '{real}' \"$@\" < '{seen}'\n"
        "fi\n"
        f"exec '{real}' \"$@\"\n"))
    return wrapper, seen


def _crashing_da_read(tmp_path) -> Path:
    """A da-read whose `alertmanager` reading dies like a Go panic (exit 2,
    no verdict); every other reading is the real one's."""
    real = _real_da_read()
    return _executable(tmp_path / "da-read-crash", (
        "#!/bin/sh\n"
        'if [ "$1" = alertmanager ]; then\n'
        "  echo 'panic: runtime error' >&2\n"
        "  exit 2\n"
        "fi\n"
        f"exec '{real}' \"$@\"\n"))


def _accepting_amtool(tmp_path) -> Path:
    """A directory holding an `amtool` that accepts anything and records
    that it ran — #3793: the gate never asks it."""
    d = tmp_path / "amtool-bin"
    d.mkdir()
    _executable(d / "amtool",
                f"#!/bin/sh\n: > '{tmp_path / 'amtool-ran'}'\necho SUCCESS\nexit 0\n")
    return d


def _gar(argv, tmp_path, *, path: Path | None = None, da_read: str | None = None):
    """Run the CLI with PATH = *path* (default: an empty directory) and, when
    given, ``$DA_READ_BINARY`` = *da_read* ("" = none)."""
    if path is None:
        path = tmp_path / "empty-path"
        path.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=str(path))
    if da_read is not None:
        env["DA_READ_BINARY"] = da_read
    return subprocess.run([sys.executable, "-s", str(_GAR), *argv],
                          capture_output=True, timeout=180, env=env)


# ── --output-configmap ──────────────────────────────────────────────
@_POSIX_ONLY
class TestOutputConfigmapGate:

    def test_accepted_writes_and_the_loader_read_the_written_text(self, tenant_dir, tmp_path):
        """The loader must have read the alertmanager.yml INSIDE the file that
        was written — not a re-render of it."""
        spy, seen = _spying_da_read(tmp_path)
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], tmp_path, da_read=str(spy))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert out.is_file()
        assert _ACCEPTED in err, err
        written_am = yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"]
        assert seen.read_bytes() == written_am.encode("utf-8")

    def test_refused_writes_nothing_and_exits_1(self, refused_dir, tmp_path):
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(refused_dir), "--output-configmap",
                  "-o", str(out)], tmp_path)
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert not out.exists(), "a config Alertmanager's loader refused was written anyway"
        assert _REFUSED in err and "http://[1]/" in err, err
        assert "nothing was written" in err, err

    def test_refused_prints_nothing_to_stdout_either(self, refused_dir, tmp_path):
        """Without -o the ConfigMap goes to stdout — that is an emission too."""
        r = _gar(["--config-dir", str(refused_dir), "--output-configmap"], tmp_path)
        assert r.returncode == 1, r.stderr.decode("utf-8", "replace")
        assert b"apiVersion" not in r.stdout
        assert b"alertmanager.yml" not in r.stdout

    def test_no_da_read_is_a_caller_error(self, tenant_dir, tmp_path):
        """No reader → an environment failure (2), not a config verdict (1)
        (the run stops at the first reading it needs, before the gate)."""
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], tmp_path, da_read="")
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 2, err
        assert not out.exists()
        assert "da-read was not found" in err, err

    def test_crashing_da_read_is_a_caller_error_not_a_rejection(self, tenant_dir, tmp_path):
        """A Go panic exits 2 with no verdict: the reader broke, the config
        was never judged — environment (2), not config wrong (1)."""
        out = tmp_path / "cm.yaml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                  "-o", str(out)], tmp_path, da_read=str(_crashing_da_read(tmp_path)))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 2, err
        assert not out.exists()
        assert "the way Alertmanager does" in err and "nothing was written" in err, err

    def test_an_amtool_on_path_changes_nothing(self, tenant_dir, tmp_path):
        """#3793: rc, bytes and stderr identical with and without an amtool on
        PATH, and the amtool is never run."""
        with_amtool = tmp_path / "with.yaml"
        without = tmp_path / "without.yaml"
        r1 = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                   "-o", str(with_amtool)], tmp_path, path=_accepting_amtool(tmp_path))
        r2 = _gar(["--config-dir", str(tenant_dir), "--output-configmap",
                   "-o", str(without)], tmp_path)
        err1 = r1.stderr.decode("utf-8", "replace")
        assert r1.returncode == r2.returncode == 0, err1
        assert not (tmp_path / "amtool-ran").exists(), "the gate ran amtool"
        assert with_amtool.read_bytes() == without.read_bytes()
        assert err1.replace(str(with_amtool), "OUT") == \
            r2.stderr.decode("utf-8", "replace").replace(str(without), "OUT")


# ── --output-alertmanager ───────────────────────────────────────────
@_POSIX_ONLY
class TestAlertmanagerModeIsGated:
    """#1211 gap 2: --output-alertmanager emits a whole config and goes
    through the same gate as --output-configmap: the loader reads exactly the
    text written, and its refusal writes nothing."""

    def test_the_loader_reads_the_emitted_alertmanager_yml(self, tenant_dir, tmp_path):
        spy, seen = _spying_da_read(tmp_path)
        out = tmp_path / "alertmanager.yml"
        r = _gar(["--config-dir", str(tenant_dir), "--output-alertmanager",
                  "-o", str(out)], tmp_path, da_read=str(spy))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert out.is_file()
        assert seen.exists(), "the loader was not asked about the emitted config"
        assert seen.read_bytes() == out.read_bytes(), (
            "the loader read other text than the one written")

    def test_a_refusal_writes_nothing(self, refused_dir, tmp_path):
        out = tmp_path / "alertmanager.yml"
        r = _gar(["--config-dir", str(refused_dir), "--output-alertmanager",
                  "-o", str(out)], tmp_path)
        assert r.returncode == 1, r.stderr.decode("utf-8", "replace")
        assert not out.exists(), "a config the loader refused was written"


# ── --apply (in-process: no shell stub, runs on every platform) ─────
def _existing_cm_json() -> str:
    return json.dumps({"data": {"alertmanager.yml": yaml.dump({
        "route": {"receiver": "default", "routes": []},
        "receivers": [{"name": "default"}],
        "inhibit_rules": [],
    })}})


def _fake_cluster(monkeypatch, *, curl_rc: int = 0):
    """Patch `_run_binary` (kubectl, curl); return the argv log. Alertmanager's
    loader is the real da-read."""
    calls: list[list[str]] = []

    def fake(argv, *, timeout, stdin_text=None):
        calls.append(list(argv))
        if argv[:2] == ["kubectl", "get"]:
            return subprocess.CompletedProcess(argv, 0, stdout=_existing_cm_json(), stderr="")
        if argv[0] == "curl":
            return subprocess.CompletedProcess(argv, curl_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(render, "_run_binary", fake)
    return calls


def _spy_reading(monkeypatch) -> list[str]:
    """Record every text handed to Alertmanager's loader; the real one reads it."""
    seen: list[str] = []
    real = render.alertmanager_reading

    def spy(text):
        seen.append(text)
        return real(text)
    monkeypatch.setattr(render, "alertmanager_reading", spy)
    return seen


_ROUTES = [{"receiver": "tenant-t1", "matchers": ['tenant="t1"']}]
_RECEIVERS = [{"name": "tenant-t1", "webhook_configs": [{"url": "https://x.example.com/h"}]}]
_REFUSED_RECEIVERS = [{"name": "tenant-t1", "webhook_configs": [{
    "url": "https://x.example.com/h",
    "http_config": {"follow_redirects": "CANARY3689"}}]}]


def _kubectl_writes(calls):
    return [c for c in calls if c[:2] in (["kubectl", "apply"], ["kubectl", "create"])]


class TestApplyGate:

    def _run_apply_mode(self, receivers=_RECEIVERS):
        with pytest.raises(SystemExit) as exc:
            gar._apply_mode(_ROUTES, receivers, [], "monitoring",
                            "alertmanager-config", yes_flag=True)
        return exc.value.code

    def test_refused_config_never_reaches_kubectl_apply(self, monkeypatch, capsys):
        """The merged text goes to Alertmanager's loader before the cluster is
        touched; its refusal is rc 1, without the refused value."""
        calls = _fake_cluster(monkeypatch)
        assert self._run_apply_mode(_REFUSED_RECEIVERS) == 1
        assert _kubectl_writes(calls) == [], calls
        assert not any(c[0] == "curl" for c in calls), calls
        err = capsys.readouterr().err
        assert _REFUSED in err and "nothing was applied" in err, err
        assert "CANARY" not in err, err

    def test_the_loader_read_the_exact_text_passed_to_kubectl(self, monkeypatch):
        calls = _fake_cluster(monkeypatch)
        seen = _spy_reading(monkeypatch)
        assert self._run_apply_mode() == 0
        create = next(c for c in calls if c[:2] == ["kubectl", "create"])
        literal = next(a for a in create if a.startswith("--from-literal=alertmanager.yml="))
        # earlier readings are the cluster's existing config (the merge source)
        assert seen[-1] == literal[len("--from-literal=alertmanager.yml="):]

    def test_no_da_read_is_exit_2_and_applies_nothing(self, monkeypatch, tmp_path):
        calls = _fake_cluster(monkeypatch)
        monkeypatch.setenv("DA_READ_BINARY", str(tmp_path / "no-such-da-read"))
        assert self._run_apply_mode() == 2
        assert _kubectl_writes(calls) == [], calls

    def test_reload_failure_is_exit_2(self, monkeypatch, capsys):
        calls = _fake_cluster(monkeypatch, curl_rc=22)
        assert self._run_apply_mode() == 2
        assert any(c[0] == "curl" for c in calls), "exit 2 came from before the reload"
        assert "ERROR: Alertmanager reload failed" in capsys.readouterr().err

    def test_control_reload_success_is_exit_0(self, monkeypatch, capsys):
        """Without this, the row above passes for a tool that always exits 2."""
        calls = _fake_cluster(monkeypatch, curl_rc=0)
        assert self._run_apply_mode() == 0
        assert any(c[:2] == ["kubectl", "apply"] for c in calls), calls
        assert _ACCEPTED in capsys.readouterr().err


# ── #2260: --validate goes through the same gate ────────────────────
# Before #2260, --validate printed OK at rc 0 for a tree that
# --output-configmap then refused (`http://[1]/`). It assembles the config on
# the BUILT-IN base (`--validate --base-config` stays rc 2, #1616) and hands it
# to `alertmanager_check` before printing OK — the one gate the write paths
# call too.
@_POSIX_ONLY
class TestValidateGate:

    def test_refused_is_exit_1_and_never_says_ok(self, refused_dir, tmp_path):
        r = _gar(["--config-dir", str(refused_dir), "--validate"], tmp_path)
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert b"OK: all configs valid" not in r.stdout
        assert _REFUSED in err and "http://[1]/" in err, err

    def test_the_loader_read_a_complete_config_on_the_builtin_base(self, tenant_dir, tmp_path):
        """A complete config (root receiver from the built-in base) — not a
        fragment — and the result line names that base."""
        spy, seen = _spying_da_read(tmp_path)
        r = _gar(["--config-dir", str(tenant_dir), "--validate"], tmp_path,
                 da_read=str(spy))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 0, err
        assert b"OK: all configs valid" in r.stdout
        assert _ACCEPTED in err and "built-in default base" in err, err
        am = yaml.safe_load(seen.read_text(encoding="utf-8"))
        assert am["route"]["receiver"] == render._DEFAULT_BASE_CONFIG["route"]["receiver"]
        assert "tenant-t1" in [x["name"] for x in am["receivers"]]

    def test_no_reading_is_exit_2(self, tenant_dir, tmp_path):
        """No verdict on the config → environment (2), never a rejection."""
        r = _gar(["--config-dir", str(tenant_dir), "--validate"], tmp_path,
                 da_read=str(_crashing_da_read(tmp_path)))
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 2, err
        assert b"OK: all configs valid" not in r.stdout
        assert "the way Alertmanager does" in err, err

    def test_validate_with_base_config_is_still_a_caller_error(self, tenant_dir, tmp_path):
        """#1616: --validate never reads --base-config; #2260 does not change that."""
        base = tmp_path / "base.yml"
        base.write_text("route:\n  receiver: default\nreceivers:\n  - name: default\n",
                        encoding="utf-8")
        r = _gar(["--config-dir", str(tenant_dir), "--validate",
                  "--base-config", str(base)], tmp_path)
        assert r.returncode == 2, r.stderr.decode("utf-8", "replace")


class TestAssemblyValueErrorIsAVerdict:
    """An invariant `assemble_configmap` refuses (ValueError) is a CONFIG
    verdict: FAIL at rc 1, not a traceback."""

    def test_validate_mode(self, monkeypatch, capsys):
        def boom(*_a, **_k):
            raise ValueError("fake invariant #2260")
        # #2311: --validate assembles inside `_grar_render.evaluate_generated_config`
        # (shared with validate-config), which resolves the name in its own module.
        monkeypatch.setattr(render, "assemble_configmap", boom)
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
                  "--base-config", str(base), "-o", str(out)], tmp_path)
        err = r.stderr.decode("utf-8", "replace")
        assert r.returncode == 1, err
        assert "Traceback" not in err, err
        assert "FAIL:" in err and "invariant violated" in err, err
        assert not out.exists()


# ── the repo's own conf.d ───────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]


@_POSIX_ONLY
def test_repo_conf_d_is_accepted(tmp_path):
    r = _gar(["--config-dir", str(_REPO / "components" / "threshold-exporter"
                                  / "config" / "conf.d"), "--validate"], tmp_path)
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 0, err
    assert _ACCEPTED in err, err
