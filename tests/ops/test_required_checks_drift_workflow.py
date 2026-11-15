"""`.github/workflows/required-checks-drift.yml`, its two scripted steps run
whole (#3946).

The compare step used to read the script's exit status on a line of its own.
The step has no `shell:`, so it runs as `bash -e`: when the script exited 1
(they differ) or 2 (nothing could be compared) the step ended on that line —
no drift text in the log, no summary, no `rc` step output, no `::error::`.
Only exit 0, which has nothing to say, ran to the end.

Each test here hands the ENTIRE `run:` text to bash under the flags of the
step's own shell (tests/_workflow_steps.py), with stand-ins for `gh` and
`python3`. The script's comparison is covered by tests/ops/test_required_checks_ssot.py;
that no other workflow has the shape, by tests/ops/test_workflow_status_reads.py.

The verdict is not the exit code alone (#3946, second round): the interpreter
exits 1 when the script cannot start, and that used to be reported as "they
differ". The script prints one fixed line per verdict; the step believes an
exit code only together with its line.

⚠️ Not covered: the workflow running on GitHub — whether `failure()` and
`steps.compare.outputs.rc` evaluate as written is not exercised by any test.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml
from _workflow_steps import REPO_ROOT, effective_shell, run_step

WORKFLOW = REPO_ROOT / ".github/workflows/required-checks-drift.yml"
SCRIPT = REPO_ROOT / "scripts/ops/required_checks_drift.py"
TITLE = "[ci] required checks drift: .github/required-checks.yaml ≠ branch protection"


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _doc()["jobs"]
    assert list(jobs) == ["drift"], f"jobs are now {list(jobs)}; re-read this module"
    return jobs["drift"]


def _compare_step() -> dict:
    found = [s for s in _job()["steps"] if s.get("id") == "compare"]
    assert len(found) == 1, found
    return found[0]


def _issue_step() -> dict:
    found = [s for s in _job()["steps"] if "gh issue" in str(s.get("run", ""))]
    assert len(found) == 1, [s.get("name") for s in found]
    return found[0]


def _script():
    spec = importlib.util.spec_from_file_location("required_checks_drift_for_wf", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drift = _script()
AGREE, DIFFER = drift.AGREE_LINE, drift.DIFFER_LINE


def _literal_env(step: dict) -> dict:
    """The step's `env:` entries that are plain text (expressions are the
    runner's to fill in)."""
    return {k: v for k, v in (step.get("env") or {}).items() if "${{" not in str(v)}


def _compare(tmp_path: Path, script_rc: int, says: str = "", gh_rc: int = 0):
    """Run the whole compare step with a `python3` that prints `says` (then a
    line on stdout and one on stderr) and exits `script_rc`. Returns the
    process, the summary text and the `$GITHUB_OUTPUT` lines as a dict (None
    for a file never written)."""
    step = _compare_step()
    assert "python3 -X utf8 scripts/ops/required_checks_drift.py" in step["run"]
    out, summary = tmp_path / "out", tmp_path / "summary"
    said = tmp_path / "said.txt"
    said.write_text(says, encoding="utf-8")
    proc = run_step(
        step["run"], effective_shell(_doc(), _job(), step), tmp_path,
        {**_literal_env(step), "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out),
         "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"},
        {"gh": f'echo "GH: $*" >&2\necho "{{}}"\nexit {gh_rc}\n',
         "python3": f'cat "{said}"\necho "FOUND: a difference"\n'
                    f'echo "WHY: on stderr" >&2\nexit {script_rc}\n'})
    outputs = (dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
               if out.exists() else None)
    return proc, (summary.read_text(encoding="utf-8") if summary.exists() else None), outputs


_DISAGREE = "::error::.github/required-checks.yaml and branch protection disagree"
_NOTHING = "::error::required_checks_drift.py compared nothing (rc={rc})"


@pytest.mark.parametrize("script_rc, says, verdict, step_rc", [
    (0, AGREE + "\n", "agree", 0),
    (1, DIFFER + "\n", "differ", 1),
    (2, "", "nothing", 2),
    (3, "", "nothing", 3),
    (120, "", "nothing", 120),
    # ⛔ The interpreter's own exit 1: `import yaml` failed, a SyntaxError.
    (1, "", "nothing", 2),
    (1, "Traceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'\n",
     "nothing", 2),
    (0, "", "nothing", 2),
    (1, AGREE + "\n", "nothing", 2),
    (0, DIFFER + "\n", "nothing", 2),
    (2, DIFFER + "\n", "nothing", 2),
    (1, f"x {DIFFER}\n", "nothing", 2),
    (1, f"{DIFFER} (not really)\n", "nothing", 2),
], ids=["agree", "differ", "nothing-compared", "unexpected", "interpreter-120",
        "exit-1-without-the-line", "exit-1-traceback", "exit-0-without-the-line",
        "exit-1-with-the-agree-line", "exit-0-with-the-differ-line",
        "exit-2-with-the-differ-line", "line-inside-a-longer-line", "line-with-a-suffix"])
def test_the_verdict_is_the_scripts_line_and_every_exit_is_reported(
        tmp_path, script_rc, says, verdict, step_rc) -> None:
    """On every exit the script's output is in the log, the summary is
    written, `rc` and `verdict` are step outputs and the annotation names the
    right finding. ⛔ Before #3946 the step ended on the script's line for
    every non-zero exit; and exit 1 alone used to mean "they differ", which is
    also what an interpreter that could not start the script exits with."""
    proc, summary, out = _compare(tmp_path, script_rc, says)
    assert proc.returncode == step_rc, (proc.stdout, proc.stderr)
    # stdout AND stderr of the script: an rc-2 reason is printed on stderr.
    assert "FOUND: a difference\n" in proc.stdout and "WHY: on stderr\n" in proc.stdout
    assert summary is not None, "the summary was never written"
    assert summary.splitlines()[0] == (
        f"### Required checks vs branch protection ({verdict}, rc={script_rc})")
    assert "- FOUND: a difference" in summary and "- WHY: on stderr" in summary
    assert out == {"rc": str(script_rc), "verdict": verdict}, out
    errors = [line for line in proc.stdout.splitlines() if line.startswith("::error::")]
    expected = {"agree": [], "differ": [_DISAGREE],
                "nothing": [_NOTHING.format(rc=script_rc)]}[verdict]
    assert len(errors) == len(expected), errors
    for want, got in zip(expected, errors):
        assert got.startswith(want), errors
    assert ("disagree" in proc.stdout) is (verdict == "differ")


def test_branch_protection_that_cannot_be_read_is_exit_2_before_the_script(tmp_path) -> None:
    proc, summary, out = _compare(tmp_path, 0, AGREE + "\n", gh_rc=1)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert out == {"rc": "2", "verdict": "nothing"} and summary is None
    assert "::error::could not read repos/o/r/branches/main — nothing was compared" in proc.stdout
    assert "FOUND" not in proc.stdout, "the script must not run on an answer gh did not give"
    assert "GH: api repos/o/r/branches/main" in proc.stderr


def test_the_workflow_and_the_script_use_the_same_two_lines() -> None:
    """The workflow cannot import the script's constants, so it carries them
    as `env:` text. This is what keeps the two copies one string."""
    env = _compare_step()["env"]
    assert (env["AGREE_LINE"], env["DIFFER_LINE"]) == (AGREE, DIFFER)
    assert AGREE != DIFFER and "\n" not in AGREE + DIFFER
    run = _compare_step()["run"]
    assert 'grep -qxF -- "$AGREE_LINE"' in run and 'grep -qxF -- "$DIFFER_LINE"' in run


@pytest.mark.parametrize("live, rc, line", [
    ({"A": 15368}, 0, AGREE), ({"A": 15368, "B": 15368}, 1, DIFFER), (None, 2, None),
], ids=["agree", "differ", "unreadable"])
def test_the_script_prints_its_verdict_as_a_whole_line(tmp_path, capsys, live, rc, line) -> None:
    """…and only after the comparison ran to its end: exit 2 prints neither."""
    ssot = tmp_path / "ssot.yaml"
    ssot.write_text(yaml.safe_dump({"app_id": 15368, "enforcement_level": "everyone",
                                    "required": [{"name": "A", "kind": "job"}]}),
                    encoding="utf-8")
    branch = tmp_path / "branch.json"
    branch.write_text(json.dumps({} if live is None else {"protection": {
        "enabled": True, "required_status_checks": {
            "enforcement_level": "everyone",
            "checks": [{"context": c, "app_id": a} for c, a in live.items()]}}}),
        encoding="utf-8")
    assert drift.main([str(branch), str(ssot)]) == rc
    lines = capsys.readouterr().out.splitlines()
    assert [text for text in lines if text in (AGREE, DIFFER)] == ([line] if line else []), lines
    if rc == 1:
        assert lines[0] == DIFFER and len(lines) > 1, "the differences follow the verdict line"


def test_the_compare_step_shell_is_one_the_helper_models() -> None:
    """Pins which case is live: a `shell:` added here changes the flags the
    tests above run under, and that should be a visible change."""
    assert effective_shell(_doc(), _job(), _compare_step()) is None


# ── the tracking issue ──────────────────────────────────────────────────────

def _open_issue(tmp_path: Path, verdict: str, compare_rc: str = "", *, drift: str | None,
                existing=()):
    """Run the whole issue step. `gh` records every call as one JSON line and
    answers `issue list` with `existing`."""
    step = _issue_step()
    calls = tmp_path / "calls.jsonl"
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps(list(existing)), encoding="utf-8")
    if drift is not None:
        (tmp_path / "drift.txt").write_text(drift, encoding="utf-8")
    real_python = shutil.which("python3")
    assert real_python, "needs python3"
    recorder = (f'exec "{real_python}" -c \'import json, sys\n'
                f'open(sys.argv[1], "a", encoding="utf-8").write(json.dumps(sys.argv[3:]) + "\\n")\n'
                f'if sys.argv[3:5] == ["issue", "list"]:\n'
                f'    sys.stdout.write(open(sys.argv[2], encoding="utf-8").read())\n'
                f'\' "{calls}" "{listing}" "$@"\n')
    proc = run_step(
        step["run"], effective_shell(_doc(), _job(), step), tmp_path,
        {"RUNNER_TEMP": str(tmp_path), "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x",
         "RUN_URL": "https://example.invalid/run/1", "COMPARE_RC": compare_rc,
         "COMPARE_VERDICT": verdict},
        {"gh": recorder})
    rows = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    return proc, rows


def _body(call: list[str]) -> str:
    return call[call.index("--body") + 1]


@pytest.mark.parametrize("verdict, compare_rc, drift, first_line", [
    ("differ", "1", DIFFER + "\nFOUND: a difference\n", "The file and branch protection DIFFER."),
    ("nothing", "2", "WHY: unreadable\n", "NOTHING WAS COMPARED (rc=2): "),
    ("nothing", "1", "ModuleNotFoundError: No module named 'yaml'\n",
     "NOTHING WAS COMPARED (rc=1): "),
    ("nothing", "2", None, "NOTHING WAS COMPARED (rc=2): "),
    ("", "", None, "NO RESULT (verdict=''): "),
    ("agree", "0", AGREE + "\n", "NO RESULT (verdict='agree'): "),
    ("", "1", DIFFER + "\n", "NO RESULT (verdict=''): "),
], ids=["differ", "nothing-compared", "script-could-not-start", "gh-failed-no-drift-file",
        "compare-never-reported", "a-later-step-failed", "rc-1-is-not-read-again"])
def test_the_issue_says_which_kind_of_failure_it_was(tmp_path, verdict, compare_rc, drift,
                                                     first_line) -> None:
    """`failure()` is true for any failed step and the title is fixed, so the
    body is the only place the three are told apart. ⛔ Only the compare
    step's `differ` is a finding about the file — this step does not look at
    the exit code or the drift text to decide again."""
    proc, calls = _open_issue(tmp_path, verdict, compare_rc, drift=drift)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert [c[:2] for c in calls] == [["issue", "list"], ["issue", "create"]], calls
    create = calls[1]
    assert create[create.index("--title") + 1] == TITLE
    body = _body(create)
    assert body.splitlines()[0].startswith(first_line), body
    assert ("says nothing about the file" in body.splitlines()[0]) is (verdict != "differ")
    assert "Run: https://example.invalid/run/1" in body
    assert (drift.strip() in body) if drift else body.rstrip().endswith("run/1")


def test_an_open_issue_with_the_exact_title_is_commented_not_duplicated(tmp_path) -> None:
    existing = [{"number": 7, "title": TITLE + " (old)"}, {"number": 9, "title": TITLE}]
    proc, calls = _open_issue(tmp_path, "differ", "1", drift="x\n", existing=existing)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert [c[:3] for c in calls] == [["issue", "list", "--repo"], ["issue", "comment", "9"]]
    assert _body(calls[1]).startswith("The file and branch protection DIFFER.")


def test_the_issue_step_reads_the_compare_steps_verdict_and_only_runs_on_schedule() -> None:
    step = _issue_step()
    assert step["env"]["COMPARE_VERDICT"] == "${{ steps.compare.outputs.verdict }}"
    assert step["env"]["COMPARE_RC"] == "${{ steps.compare.outputs.rc }}"
    assert step["if"] == "failure() && github.event_name == 'schedule'"
    assert "${{" not in step["run"], "expressions reach the script through env only"
    assert 'case "$COMPARE_VERDICT" in' in step["run"] and 'case "$COMPARE_RC"' not in step["run"]
