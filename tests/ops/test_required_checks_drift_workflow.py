"""`.github/workflows/required-checks-drift.yml`, its two scripted steps run
whole (#3946).

The compare step used to read the script's exit status on a line of its own.
The step has no `shell:`, so it runs as `bash -e`: when the script exited 1
(they differ) or 2 (nothing could be compared) the step ended on that line —
no drift text in the log, no summary, no `rc` step output, no `::error::`.
Only exit 0, which has nothing to say, ran to the end.

Each test here hands the ENTIRE `run:` text to bash under the flags of the
step's own shell (tests/_workflow_steps.py), with stand-ins for `gh` and
`python3`. The script itself is covered by tests/ops/test_required_checks_ssot.py;
that no other workflow has the shape, by tests/ops/test_workflow_status_reads.py.

⚠️ Not covered: the workflow running on GitHub — whether `failure()` and
`steps.compare.outputs.rc` evaluate as written is not exercised by any test.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from _workflow_steps import REPO_ROOT, effective_shell, run_step

WORKFLOW = REPO_ROOT / ".github/workflows/required-checks-drift.yml"
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


def _compare(tmp_path: Path, script_rc: int, gh_rc: int = 0):
    """Returns the process, the summary text (None if never written) and the
    `$GITHUB_OUTPUT` text (None if never written)."""
    step = _compare_step()
    assert "python3 -X utf8 scripts/ops/required_checks_drift.py" in step["run"]
    out, summary = tmp_path / "out", tmp_path / "summary"
    proc = run_step(
        step["run"], effective_shell(_doc(), _job(), step), tmp_path,
        {"RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out),
         "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"},
        {"gh": f'echo "GH: $*" >&2\necho "{{}}"\nexit {gh_rc}\n',
         "python3": f'echo "FOUND: a difference"\necho "WHY: on stderr" >&2\nexit {script_rc}\n'})

    def text(path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.exists() else None
    return proc, text(summary), text(out)


@pytest.mark.parametrize("script_rc, annotation", [
    (0, None),
    (1, "::error::.github/required-checks.yaml and branch protection disagree (rc=1)"),
    (2, "::error::required_checks_drift.py compared nothing (rc=2)"),
    (3, "::error::required_checks_drift.py compared nothing (rc=3)"),
    (120, "::error::required_checks_drift.py compared nothing (rc=120)"),
], ids=["agree", "differ", "nothing-compared", "unexpected", "interpreter"])
def test_whatever_the_script_exits_the_step_reports_it(tmp_path, script_rc, annotation) -> None:
    """The step's exit is the script's, and on every exit the drift text is in
    the log, the summary is written, `rc` is a step output and the annotation
    (if any) names the right finding. ⛔ Before #3946 every row but the first
    failed on all four."""
    proc, summary, out = _compare(tmp_path, script_rc)
    assert proc.returncode == script_rc, (proc.stdout, proc.stderr)
    # stdout AND stderr of the script: an rc-2 reason is printed on stderr.
    assert "FOUND: a difference\n" in proc.stdout and "WHY: on stderr\n" in proc.stdout
    assert summary is not None, "the summary was never written"
    assert summary.splitlines()[0] == f"### Required checks vs branch protection (rc={script_rc})"
    assert "- FOUND: a difference" in summary and "- WHY: on stderr" in summary
    assert out == f"rc={script_rc}\n", out
    errors = [line for line in proc.stdout.splitlines() if line.startswith("::error::")]
    if annotation is None:
        assert errors == []
    else:
        assert len(errors) == 1 and errors[0].startswith(annotation), errors


def test_a_difference_is_not_announced_as_nothing_compared_or_the_reverse(tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    differ = _compare(tmp_path / "a", 1)[0].stdout
    nothing = _compare(tmp_path / "b", 2)[0].stdout
    assert "disagree" in differ and "compared nothing" not in differ
    assert "compared nothing" in nothing and "disagree" not in nothing


def test_branch_protection_that_cannot_be_read_is_exit_2_before_the_script(tmp_path) -> None:
    proc, summary, out = _compare(tmp_path, 0, gh_rc=1)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert out == "rc=2\n" and summary is None
    assert "::error::could not read repos/o/r/branches/main — nothing was compared" in proc.stdout
    assert "FOUND" not in proc.stdout, "the script must not run on an answer gh did not give"
    assert "GH: api repos/o/r/branches/main" in proc.stderr


def test_the_compare_step_shell_is_one_the_helper_models() -> None:
    """Pins which case is live: a `shell:` added here changes the flags the
    tests above run under, and that should be a visible change."""
    assert effective_shell(_doc(), _job(), _compare_step()) is None


# ── the tracking issue ──────────────────────────────────────────────────────

def _open_issue(tmp_path: Path, compare_rc: str, *, drift: str | None, existing=()):
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
         "RUN_URL": "https://example.invalid/run/1", "COMPARE_RC": compare_rc},
        {"gh": recorder})
    rows = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    return proc, rows


def _body(call: list[str]) -> str:
    return call[call.index("--body") + 1]


@pytest.mark.parametrize("compare_rc, drift, first_line", [
    ("1", "FOUND: a difference\n", "The file and branch protection DIFFER (rc=1)."),
    ("2", "WHY: unreadable\n", "NOTHING WAS COMPARED (rc=2): "),
    ("2", None, "NOTHING WAS COMPARED (rc=2): "),
    ("", None, "NO RESULT (rc=''): "),
    ("3", "boom\n", "NO RESULT (rc='3'): "),
], ids=["differ", "nothing-compared", "gh-failed-no-drift-file", "compare-never-reported",
        "unexpected"])
def test_the_issue_says_which_kind_of_failure_it_was(tmp_path, compare_rc, drift,
                                                     first_line) -> None:
    """`failure()` is true for any failed step and the title is fixed, so the
    body is the only place the three are told apart. ⛔ Only rc 1 is a finding
    about the file: "nothing was compared" and "no result" must say so, not
    be filed as a drift."""
    proc, calls = _open_issue(tmp_path, compare_rc, drift=drift)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert [c[:2] for c in calls] == [["issue", "list"], ["issue", "create"]], calls
    create = calls[1]
    assert create[create.index("--title") + 1] == TITLE
    body = _body(create)
    assert body.splitlines()[0].startswith(first_line), body
    if compare_rc != "1":
        assert "says nothing about the file" in body.splitlines()[0]
    assert "Run: https://example.invalid/run/1" in body
    assert (drift.strip() in body) if drift else body.rstrip().endswith("run/1")


def test_an_open_issue_with_the_exact_title_is_commented_not_duplicated(tmp_path) -> None:
    existing = [{"number": 7, "title": TITLE + " (old)"}, {"number": 9, "title": TITLE}]
    proc, calls = _open_issue(tmp_path, "1", drift="x\n", existing=existing)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert [c[:3] for c in calls] == [["issue", "list", "--repo"], ["issue", "comment", "9"]]
    assert _body(calls[1]).startswith("The file and branch protection DIFFER (rc=1).")


def test_the_issue_step_reads_the_compare_steps_output_and_only_runs_on_schedule() -> None:
    step = _issue_step()
    assert step["env"]["COMPARE_RC"] == "${{ steps.compare.outputs.rc }}"
    assert step["if"] == "failure() && github.event_name == 'schedule'"
    assert "${{" not in step["run"], "expressions reach the script through env only"
