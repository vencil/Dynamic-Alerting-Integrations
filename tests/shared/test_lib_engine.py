"""_lib_engine: an engine run counts only when its output proves it ran (#3611).

Stand-in engines on PATH (shell scripts) drive the real subprocess path. The
outputs they print are the shapes the pinned engines were measured to print
(kube-linter v0.7.4, hadolint v2.12.0, pint 0.86.0, 2026-11-06):

* kube-linter, clean object: rc 0, ``{"Checks":[...],"Reports":null,"Summary":{...}}``
* kube-linter, empty file / dir: rc 0, empty stdout, ``Warning: no valid objects found.``
* kube-linter, missing path or config: rc 1, empty stdout, ``Error: ...``
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from _tree import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
import _lib_engine as E  # noqa: E402

_KL_CLEAN = '{"Checks":[],"Reports":null,"Summary":{"ChecksStatus":"Passed"}}'
_KL_FINDING = ('{"Checks":[],"Reports":[{"Check":"host-pid"}],'
               '"Summary":{"ChecksStatus":"Failed"}}')


def _stand_in(tmp_path: Path, name: str, rc: int, stdout: str = "",
              stderr: str = "", sleep: int = 0) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(
        "#!/bin/sh\n"
        + (f"/bin/sleep {sleep}\n" if sleep else "")
        + f"printf '%s' '{stdout}'\nprintf '%s' '{stderr}' >&2\nexit {rc}\n",
        encoding="utf-8")
    exe.chmod(0o755)
    return bin_dir


#: The argv each proof requires (it reads the output those flags ask for).
_ARGS = {"kube-linter": ["lint", "--format", "json", "x"],
         "hadolint": ["--no-fail", "--format", "json", "x"]}


def _run(monkeypatch, bin_dir, engine, sub, *, timeout=30):
    monkeypatch.setenv("PATH", str(bin_dir))
    loc = E.locate(engine)
    assert isinstance(loc, E.Located), loc
    return E.run(loc, sub, _ARGS.get(engine, ["x"]), timeout=timeout)


# ── kube-linter: the #3611 fake success ─────────────────────────────────────

@pytest.mark.parametrize("rc, stderr", [
    (0, "Warning: no valid objects found."),     # linted nothing
    (1, 'Error: loading from path "x": no such file'),  # could not load input
], ids=["no-objects-rc0", "unloadable-rc1"])
def test_kube_linter_without_a_report_is_no_verdict(tmp_path, monkeypatch, rc, stderr):
    """Both used to be read as a clean scan ([] reports, rc 0)."""
    r = _run(monkeypatch, _stand_in(tmp_path, "kube-linter", rc, "", stderr),
             "kube-linter", "lint")
    assert isinstance(r, E.NoVerdict), r
    assert "did not run" in r.reason and stderr in r.detail


def test_kube_linter_clean_report_is_ran_with_no_findings(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "kube-linter", 0, _KL_CLEAN),
             "kube-linter", "lint")
    assert isinstance(r, E.Ran) and r.data == []


def test_kube_linter_findings_are_ran(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "kube-linter", 1, _KL_FINDING),
             "kube-linter", "lint")
    assert isinstance(r, E.Ran) and r.data == [{"Check": "host-pid"}]


@pytest.mark.parametrize("rc, stdout", [
    (0, "not json"), (0, "{}"), (0, '{"Reports":[]}'), (2, _KL_CLEAN),
    (0, '{"Reports":{},"Summary":{}}'),
], ids=["not-json", "object-without-keys", "no-summary", "rc2", "reports-not-list"])
def test_kube_linter_output_that_is_not_a_report_is_no_verdict(tmp_path, monkeypatch, rc, stdout):
    r = _run(monkeypatch, _stand_in(tmp_path, "kube-linter", rc, stdout),
             "kube-linter", "lint")
    assert isinstance(r, E.NoVerdict), r


# ── helm template ───────────────────────────────────────────────────────────

def test_helm_empty_render_is_no_verdict(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "helm", 0, "  \n"), "helm", "template")
    assert isinstance(r, E.NoVerdict) and "rendered nothing" in r.reason


def test_helm_render_failure_names_its_first_line(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "helm", 1, "", "Error: parse error\nmore"),
             "helm", "template")
    assert isinstance(r, E.NoVerdict) and "Error: parse error" in r.reason


def test_helm_render_is_ran(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "helm", 0, "kind: ConfigMap"), "helm", "template")
    assert isinstance(r, E.Ran) and r.data == "kind: ConfigMap"


# ── pint ────────────────────────────────────────────────────────────────────

def test_pint_without_entries_is_no_verdict(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "pint", 0, "", "level=ERROR msg=parse"),
             "pint", "lint")
    assert isinstance(r, E.NoVerdict) and "entries=" in r.reason


def test_pint_that_linted_no_entry_is_no_verdict(tmp_path, monkeypatch):
    """An include that matches nothing: pint 0.86.0 logs entries=0, rc 0."""
    r = _run(monkeypatch, _stand_in(tmp_path, "pint", 0, "", "entries=0"), "pint", "lint")
    assert isinstance(r, E.NoVerdict) and "entries=0" in r.reason


def test_a_proof_whose_flag_was_dropped_is_a_programming_error(tmp_path, monkeypatch):
    """hadolint without --no-fail exits 1 on a finding: read through the
    --no-fail proof that is a no-run, so the caller's slip must not pass."""
    monkeypatch.setenv("PATH", str(_stand_in(tmp_path, "hadolint", 0, "[]")))
    with pytest.raises(ValueError, match="--no-fail"):
        E.run(E.locate("hadolint"), "lint", ["--format", "json", "x"], timeout=30)


def test_pint_entries_and_rc_are_returned(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "pint", 1, "", "entries=400"), "pint", "lint")
    assert isinstance(r, E.Ran) and r.data == (400, 1)


# ── the run itself ──────────────────────────────────────────────────────────

def test_a_timeout_is_no_verdict(tmp_path, monkeypatch):
    r = _run(monkeypatch, _stand_in(tmp_path, "helm", 0, "kind: x", sleep=5),
             "helm", "template", timeout=1)
    assert isinstance(r, E.NoVerdict) and "did not finish" in r.reason


def test_a_binary_that_cannot_start_is_no_verdict(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "helm"
    exe.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    exe.chmod(0o755)
    r = _run(monkeypatch, bin_dir, "helm", "template")
    assert isinstance(r, E.NoVerdict) and "cannot run helm" in r.reason


def test_docker_without_a_daemon_is_no_verdict(tmp_path, monkeypatch):
    """docker on PATH is not proof the fallback runs (#3607)."""
    bin_dir = _stand_in(tmp_path, "docker", 125, "",
                        "Cannot connect to the Docker daemon")
    monkeypatch.setenv("PATH", str(bin_dir))
    loc = E.locate("kube-linter", docker_image="img@sha256:x")
    assert (loc.mode, loc.image) == ("docker", "img@sha256:x")
    r = E.run(loc, "lint", _ARGS["kube-linter"], timeout=30)
    assert isinstance(r, E.NoVerdict) and "(docker)" in r.reason
    assert "Docker daemon" in r.detail


def test_absent_engine_is_no_verdict_naming_both_ways_out(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    nv = E.locate("pint", docker_image="img")
    assert isinstance(nv, E.NoVerdict)
    assert "install the pint binary" in nv.reason and "Docker" in nv.reason
    assert "Docker" not in E.locate("pint").reason   # no fallback, none offered


def test_docker_argv_shape():
    loc = E.Located("pint", "docker", image="img")
    assert E.argv(loc, ["lint", "a"], docker_run=["-v", "r:/w"], entrypoint="pint") == [
        "docker", "run", "--rm", "-v", "r:/w", "--entrypoint", "pint", "img", "lint", "a"]
    assert E.argv(E.Located("pint", "binary", path="/b/pint"), ["lint"],
                  entrypoint="pint") == ["/b/pint", "lint"]


def test_an_unregistered_subcommand_is_a_programming_error():
    """No proof registered is not a silent pass."""
    with pytest.raises(KeyError):
        E.run(E.Located("helm", "binary", path="/bin/true"), "package", [], timeout=5)


def test_proofs_name_registered_engines_only():
    assert {engine for engine, _ in E.PROOFS} <= set(E.ENGINES)
