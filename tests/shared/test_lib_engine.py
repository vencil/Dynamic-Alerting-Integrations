"""_lib_engine: an engine run counts only when its output proves it ran (#3611).

Stand-in engines on PATH (shell scripts) drive the real subprocess path. The
outputs they print are the shapes the pinned engines were measured to print
(kube-linter v0.7.4, hadolint v2.12.0, pint 0.86.0, 2026-11-06):

* kube-linter, clean object: rc 0, ``{"Checks":[...],"Reports":null,"Summary":{...}}``
* kube-linter, empty file / dir: rc 0, empty stdout, ``Warning: no valid objects found.``
* kube-linter, missing path or config: rc 1, empty stdout, ``Error: ...``
"""
from __future__ import annotations

import json
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
         "hadolint": ["--disable-ignore-pragma", "--no-fail", "--format", "json", "x", "-"]}


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


# ── hadolint: ran, and ran with the caller's levels (#3643) ─────────────────

_HL_ARGS = ("hadolint", "--disable-ignore-pragma", "--no-fail", "--format", "json", "x", "-")
_HL_EXPECT = {"canary": {"DL3007": ["error"], "DL3026": ["error"]}}


def _hl(*findings):
    """hadolint's JSON for (file, code, level) triples (shape measured on v2.12.0)."""
    return json.dumps([{"file": f, "line": 1, "column": 1, "code": c, "level": lv,
                        "message": "m"} for f, c, lv in findings])


_HL_CANARY = (("-", "DL3007", "error"), ("-", "DL3026", "error"))


def test_hadolint_canary_at_the_registered_levels_is_ran_without_it():
    """The canary's findings prove the run and are not findings of the caller's
    files; an unregistered code on stdin (DL3059) is not read either way."""
    proof = E.PROOFS[("hadolint", "lint")]
    out = _hl(("x", "DL3018", "warning"), *_HL_CANARY, ("-", "DL3059", "info"))
    assert proof(_cp(0, out, args=_HL_ARGS), _HL_EXPECT) == [
        {"file": "x", "line": 1, "column": 1, "code": "DL3018", "level": "warning",
         "message": "m"}]
    assert proof(_cp(0, _hl(*_HL_CANARY), args=_HL_ARGS), _HL_EXPECT) == []


@pytest.mark.parametrize("stdin_findings", [
    (),                                                        # stdin never arrived / all ignored
    (("-", "DL3007", "warning"), ("-", "DL3026", "error")),    # default level: flags not applied
    (("-", "DL3026", "error"),),                               # one rule ignored
    (("-", "DL3007", "error"),),                               # no trusted registry applied
    (*_HL_CANARY, ("-", "DL3026", "error")),                   # a trusted registry not applied
    (("x", "DL3007", "error"), ("x", "DL3026", "error")),      # right findings, wrong file
], ids=["no-canary", "default-level", "rule-missing", "dl3026-missing", "dl3026-twice",
        "not-on-stdin"])
def test_hadolint_canary_that_came_back_otherwise_is_no_proof(stdin_findings):
    """#3643: a well-formed array at exit 0 is what hadolint prints when it
    dropped the config, so the array alone proves nothing about the levels."""
    proof = E.PROOFS[("hadolint", "lint")]
    out = _hl(("x", "DL3018", "warning"), *stdin_findings)
    with pytest.raises(E.NoProof, match="canary"):
        proof(_cp(0, out, args=_HL_ARGS), _HL_EXPECT)


@pytest.mark.parametrize("drop", ["--disable-ignore-pragma", "-"])
def test_hadolint_without_the_canary_s_argv_is_a_programming_error(drop):
    args = tuple(a for a in _HL_ARGS if a != drop)
    with pytest.raises(ValueError, match="proof needs"):
        E.PROOFS[("hadolint", "lint")](_cp(0, _hl(*_HL_CANARY), args=args), _HL_EXPECT)


@pytest.mark.parametrize("expect", [{}, {"canary": {}}])
def test_hadolint_without_a_canary_expectation_is_a_programming_error(expect):
    """A caller that sends no expectation would get the pre-#3643 proof back."""
    with pytest.raises(ValueError, match="canary"):
        E.PROOFS[("hadolint", "lint")](_cp(0, "[]", args=_HL_ARGS), expect)


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


# ── node / mmdc proofs (#3611 PR 2) ─────────────────────────────────────────

def _cp(rc, out="", err="", args=("x",)):
    return subprocess.CompletedProcess(list(args), rc, out, err)


def test_esbuild_parse_needs_one_result_per_input():
    proof = E.PROOFS[("node", "esbuild-parse")]
    two = '[{"path":"a","ok":true},{"path":"b","ok":false}]'
    assert len(proof(_cp(0, two), {"paths": ["b", "a"]})) == 2
    doubled = '[{"path":"a","ok":true},{"path":"a","ok":true}]'
    for rc, out, paths in [(0, two, ["a", "b", "c"]), (1, two, ["a", "b"]),
                           (0, "[]", ["a"]), (0, '[{"path":"a"}]', ["a"]),
                           (0, "nope", ["a"]), (0, doubled, ["a", "b"])]:
        with pytest.raises(E.NoProof):
            proof(_cp(rc, out), {"paths": paths})


def test_cli_playground_needs_a_non_empty_catalog():
    proof = E.PROOFS[("node", "cli-playground")]
    row = '[{"key":"k","kind":"bare","cmd":"da-tools x"}]'
    assert proof(_cp(0, row), {}) == [{"key": "k", "kind": "bare", "cmd": "da-tools x"}]
    for rc, out in [(0, "[]"), (3, row), (0, '[{"key":"k"}]')]:
        with pytest.raises(E.NoProof):
            proof(_cp(rc, out), {})


def test_mmdc_render_exit_1_is_undecided_not_a_verdict(tmp_path):
    """Exit 1 comes back as False for the caller's canary to decide — it is
    neither a NoVerdict nor a pass."""
    proof = E.PROOFS[("mmdc", "render")]
    png = tmp_path / "o.png"
    assert proof(_cp(1), {"output": str(png)}) is False
    with pytest.raises(E.NoProof, match="no image"):
        proof(_cp(0), {"output": str(png)})
    png.write_bytes(b"PNG")
    assert proof(_cp(0), {"output": str(png)}) is True
    with pytest.raises(E.NoProof):
        proof(_cp(2), {"output": str(png)})


# ── promtool proofs (#3611 PR 5) ────────────────────────────────────────────

_JUNIT_ARGS = ("promtool", "test", "rules", "--junit", "r.xml", "t.yaml")
_CASES = {"fires", "quiet"}


def _case(name, body=""):
    return f'<testcase name="{name}">{body}</testcase>'


def _report(*cases, errors=0):
    fails = sum("<failure" in c for c in cases)
    return (f'<testsuites><testsuite name="t.yaml" tests="{len(cases)}" failures="{fails}" '
            f'errors="{errors}">{"".join(cases)}</testsuite></testsuites>')


def _junit(tmp_path, text, cases=_CASES):
    path = tmp_path / "r.xml"
    path.write_text(text, encoding="utf-8")
    return {"junit": str(path), "cases": cases}


_FAIL = "<failure>x</failure>"


def test_test_rules_returns_each_group_s_pass_fail_from_the_report(tmp_path):
    """Shapes measured on promtool 3.15.1: one testcase per group, named after
    it; a failed group carries a <failure>; rc 1 when any group failed."""
    proof = E.PROOFS[("promtool", "test-rules")]
    one_fails = _junit(tmp_path, _report(_case("fires"), _case("quiet", _FAIL)))
    assert proof(_cp(1, "  FAILED:", args=_JUNIT_ARGS), one_fails) == {"fires": True, "quiet": False}
    none_fail = _junit(tmp_path, _report(_case("fires"), _case("quiet")))
    assert proof(_cp(0, args=_JUNIT_ARGS), none_fail) == {"fires": True, "quiet": True}


def test_a_failure_is_returned_as_failed_whatever_caused_it(tmp_path):
    """⛔ The proof does not read a <failure> as "the assertion did not hold":
    promtool writes an unparseable series the same way. It only reports it."""
    proof = E.PROOFS[("promtool", "test-rules")]
    parse_error = '<failure>2:10: parse error: unexpected identifier "Truex5"</failure>'
    both = _junit(tmp_path, _report(_case("fires", parse_error), _case("quiet", parse_error)))
    assert proof(_cp(1, args=_JUNIT_ARGS), both) == {"fires": False, "quiet": False}


@pytest.mark.parametrize("rc, report", [
    (1, _report('<testcase name="unknown"><error>yaml: line 1</error></testcase>', errors=1)),
    (1, None),
    (1, "not xml"),
    (0, _report(_case("fires"))),                                         # a group missing
    (1, _report(_case("fires"), _case("quiet", _FAIL), _case("other"))),  # an extra group
    (1, _report(_case("fires"), _case("fires", _FAIL))),                  # a name twice
    (0, _report(_case("fires"), _case("quiet", _FAIL))),                  # rc 0 with a failure
    (1, _report(_case("fires"), _case("quiet"))),                         # rc 1, all passed
    (0, _report(_case("fires"), _case("quiet", "<skipped/>"))),
    (1, _report(_case("fires"), "<testcase><failure>x</failure></testcase>")),  # a nameless case
    (137, _report(_case("fires"), _case("quiet", _FAIL))),               # killed after writing
    (0, "<testsuites>" + _report(_case("fires"), _case("quiet"))[12:-13] * 2 + "</testsuites>"),
], ids=["error", "no-report", "not-xml", "missing-group", "extra-group", "duplicate-group",
        "rc0-failure", "rc1-all-passed", "skipped", "nameless-case", "killed-rc137", "two-suites"])
def test_test_rules_without_the_report_of_this_run_is_no_proof(tmp_path, rc, report):
    proof = E.PROOFS[("promtool", "test-rules")]
    expect = ({"junit": str(tmp_path / "absent.xml"), "cases": _CASES} if report is None
              else _junit(tmp_path, report))
    with pytest.raises(E.NoProof):
        proof(_cp(rc, "  FAILED:\n got:[", args=_JUNIT_ARGS), expect)


def test_an_error_in_the_report_is_no_proof_whatever_the_exit_status(tmp_path):
    """The <error> itself decides — not only the rc it usually comes with — and
    its text is the reason shown (the test file promtool could not run)."""
    proof = E.PROOFS[("promtool", "test-rules")]
    report = _junit(tmp_path, _report('<testcase name="unknown"><error>yaml: line 1</error></testcase>',
                                      errors=1))
    for rc in (0, 1):
        with pytest.raises(E.NoProof, match="could not run the test: yaml: line 1"):
            proof(_cp(rc, args=_JUNIT_ARGS), report)


def test_test_rules_without_junit_in_argv_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError, match="--junit"):
        E.PROOFS[("promtool", "test-rules")](
            _cp(0, args=("promtool", "test", "rules", "t.yaml")),
            _junit(tmp_path, _report(_case("fires"), _case("quiet"))))


def test_check_rules_is_proven_by_exit_0_alone():
    proof = E.PROOFS[("promtool", "check-rules")]
    assert proof(_cp(0, "  SUCCESS: 0 rules found"), {}) is True
    with pytest.raises(E.NoProof):
        proof(_cp(1, "", "  FAILED:"), {})
