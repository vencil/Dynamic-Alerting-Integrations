"""The red-main detector (#3922): `scripts/ops/main_red_report.py` and
`.github/workflows/main-red-detector.yml`.

Three things are held here:

  * the decision table of the script — which finished run opens, comments on,
    or closes the tracking issue — driven through a fake `gh` so no test
    touches the network. ⛔ The rows that matter most are the ones where
    GitHub does not answer: a failed issue lookup must never read as "no issue
    is open" (a duplicate per failing run) and a failed runs listing must
    never read as "green" (the issue closes while main is red);
  * the workflow's shape: its `workflow_run.workflows` equals the set of
    workflows that report an entry of `.github/required-checks.yaml` (derived
    by tests/ops/test_ci_aggregate_gate_contract.py, not re-derived here), its
    permissions, and that it checks out nothing from the triggering run;
  * that every watched workflow has a `push` trigger for main, except the ones
    named in `_NO_MAIN_PUSH` with a reason — `.github/workflows/validate.yaml`
    had none until this change, so its three required checks never ran on the
    merged tree.

⚠️ Not covered: the workflow running on GitHub. `workflow_run` fires only from
the default branch, so no PR can exercise it; the `workflow_dispatch` replay
(`run_id`, dry-run by default) is the post-merge check.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ops/main_red_report.py"
DETECTOR = ROOT / ".github/workflows/main-red-detector.yml"
WORKFLOW_DIR = ROOT / ".github/workflows"
REPO = "o/r"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses resolve their module by name.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


mrr = _load("main_red_report", SCRIPT)


# ── a fake `gh api` ─────────────────────────────────────────────────────────

class FakeGh:
    """Answers `gh api` argv from a table of path → JSON (or an exit code).

    `fail` holds path prefixes that exit 1, the way `gh` does on a 5xx/403."""

    def __init__(self, *, issues=(), runs=None, jobs=None, compare=None, run=None,
                 fail=()):
        self.issues = list(issues)
        self.runs = runs or {}          # workflow file → list of runs, newest first
        self.jobs = jobs if jobs is not None else []
        self.compare = compare
        self.run = run
        self.fail = tuple(fail)
        self.calls: list[tuple[str, str]] = []
        self.written: list[tuple[str, str, dict]] = []

    def __call__(self, args, stdin):
        assert args[0] == "api" and args[1] == "-X", args
        method, path = args[2], args[3].removeprefix(f"repos/{REPO}/")
        params = dict(a.split("=", 1) for a in args[5::2]) if method == "GET" else {}
        self.calls.append((method, path))
        if any(path.startswith(prefix) for prefix in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
        if method != "GET":
            assert args[4:] == ["--input", "-"], args
            self.written.append((method, path, json.loads(stdin)))
            return self._ok({"number": 900})
        if path == "issues":
            assert params["state"] == "open", params
            page, per = int(params["page"]), int(params["per_page"])
            return self._ok(self.issues[(page - 1) * per: page * per])
        if path.startswith("actions/workflows/"):
            assert params["branch"] == "main" and params["event"] == "push", params
            return self._ok({"workflow_runs": self.runs[path.split("/")[2]]})
        if path.startswith("actions/runs/") and path.endswith("/jobs"):
            return self._ok({"jobs": self.jobs if params["page"] == "1" else []})
        if path.startswith("actions/runs/"):
            return self._ok(self.run)
        if path.startswith("compare/"):
            return self._ok(self.compare)
        raise AssertionError(f"unexpected call {args}")

    @staticmethod
    def _ok(doc):
        return subprocess.CompletedProcess([], 0, json.dumps(doc), "")


WATCHED = [mrr.Watched("CI", "ci.yml", True),
           mrr.Watched("Docs", "docs.yaml", True),
           mrr.Watched("PR only", "pr.yaml", False)]


def _run(conclusion="failure", *, workflow="CI", number=10, attempt=1, event="push",
         branch="main", sha="f" * 40):
    return mrr.Run(workflow, 5000 + number, attempt, number, event, branch, sha,
                   conclusion, f"https://example.invalid/runs/{5000 + number}")


def _api_run(number, conclusion, *, status="completed", sha=None):
    return {"id": 5000 + number, "run_number": number, "status": status,
            "conclusion": conclusion, "head_sha": sha or f"{number:040x}",
            "html_url": f"https://example.invalid/runs/{5000 + number}"}


def _issue(number=77, title=None, **extra):
    return {"number": number, "title": mrr.ISSUE_TITLE if title is None else title, **extra}


GREEN = {"ci.yml": [_api_run(12, "success")], "docs.yaml": [_api_run(8, "success")]}


def _plan(run, gh, watched=WATCHED):
    api = mrr.Api(REPO, runner=gh)
    return mrr.plan(run, watched, api), api


# ── the decision table ──────────────────────────────────────────────────────

def test_a_failure_with_no_open_issue_opens_one() -> None:
    gh = FakeGh(runs={"ci.yml": [_api_run(10, "failure"), _api_run(9, "cancelled"),
                                 _api_run(8, "success", sha="a" * 40)]},
                jobs=[{"name": "Python Tests", "conclusion": "failure"},
                      {"name": "Go Tests", "conclusion": "success"},
                      {"name": "Lint", "conclusion": "skipped"},
                      {"name": "Slow", "conclusion": "timed_out"}],
                compare={"total_commits": 2, "commits": [
                    {"sha": "b" * 40, "commit": {"message": "feat: one\n\nbody"}},
                    {"sha": "c" * 40, "commit": {"message": "fix: two @someone #12 ```"}}]})
    action, api = _plan(_run(), gh)
    assert (action.kind, action.issue) == ("open", None)
    text = action.text
    assert "`aaaaaaaaa..fffffffff`" in text, text
    assert "https://example.invalid/runs/5010" in text
    assert "`Python Tests` (failure)" in text and "`Slow` (timed_out)" in text
    assert "Go Tests" not in text and "`Lint`" not in text
    assert "bbbbbbbbb feat: one" in text and "body" not in text.split("```")[1]
    # Untrusted text stays inside the fence: the subject cannot close it.
    assert text.count("```") == 2, text
    mrr.apply(action, api)
    assert gh.written == [("POST", "issues", {"title": mrr.ISSUE_TITLE, "body": text})]
    # The per-attempt endpoint: a re-run started meanwhile must not replace
    # the jobs this report is about.
    assert ("GET", "actions/runs/5010/attempts/1/jobs") in gh.calls


def test_a_failure_with_an_open_issue_comments_on_it() -> None:
    gh = FakeGh(issues=[_issue(77)], runs={"ci.yml": [_api_run(8, "success")]})
    action, api = _plan(_run("timed_out"), gh)
    assert (action.kind, action.issue) == ("comment", 77)
    assert "concluded `timed_out`" in action.text
    mrr.apply(action, api)
    assert [(m, p) for m, p, _ in gh.written] == [("POST", "issues/77/comments")]


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped"])
@pytest.mark.parametrize("issues", [[], [_issue()]], ids=["no-issue", "issue-open"])
def test_a_run_without_a_verdict_does_nothing(conclusion, issues) -> None:
    """Neither a failure (no issue, no comment) nor a recovery (no close)."""
    gh = FakeGh(issues=issues, runs=GREEN)
    action, _ = _plan(_run(conclusion), gh)
    assert action.kind == "noop"
    assert gh.calls == [], "a run without a verdict must not even be looked up"


@pytest.mark.parametrize("kwargs", [
    {"event": "pull_request"}, {"event": "schedule"}, {"branch": "release"},
    {"event": "pull_request", "branch": "main"},
], ids=["pr", "schedule", "other-branch", "fork-branch-named-main"])
@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_only_push_runs_on_main_count(kwargs, conclusion) -> None:
    gh = FakeGh(issues=[_issue()], runs=GREEN)
    action, _ = _plan(_run(conclusion, **kwargs), gh)
    assert action.kind == "noop" and gh.calls == []


def test_a_success_with_no_open_issue_does_nothing() -> None:
    gh = FakeGh(runs=GREEN)
    action, api = _plan(_run("success"), gh)
    assert action.kind == "noop"
    mrr.apply(action, api)
    assert gh.written == []


def test_a_success_closes_the_issue_when_every_watched_workflow_is_green() -> None:
    gh = FakeGh(issues=[_issue(77)], runs={
        # A cancelled and a still-running newer run are passed over.
        "ci.yml": [_api_run(13, None, status="in_progress"), _api_run(12, "cancelled"),
                   _api_run(11, "success"), _api_run(10, "failure")],
        "docs.yaml": [_api_run(8, "success")]})
    action, api = _plan(_run("success", number=11), gh)
    assert (action.kind, action.issue) == ("close", 77)
    assert "green again as of `fffffffff`" in action.text
    assert "`PR only`" in action.text.split("Not judged")[1]
    mrr.apply(action, api)
    assert gh.written[0][:2] == ("POST", "issues/77/comments")
    assert gh.written[1] == ("PATCH", "issues/77",
                             {"state": "closed", "state_reason": "completed"})


def test_a_success_does_not_close_while_another_workflow_is_red() -> None:
    gh = FakeGh(issues=[_issue(77)], runs={
        "ci.yml": [_api_run(11, "success")],
        "docs.yaml": [_api_run(9, "cancelled"), _api_run(8, "failure", sha="d" * 40)]})
    action, api = _plan(_run("success", number=11), gh)
    assert (action.kind, action.issue) == ("comment", 77)
    assert "still red: `Docs` — `failure` at `ddddddddd`" in action.text
    mrr.apply(action, api)
    assert [(m, p) for m, p, _ in gh.written] == [("POST", "issues/77/comments")]


def test_a_success_does_not_close_while_a_workflow_has_no_verdict_at_all() -> None:
    """Every listed run cancelled is 'unknown', not green."""
    gh = FakeGh(issues=[_issue(77)], runs={
        "ci.yml": [_api_run(11, "success")],
        "docs.yaml": [_api_run(9, "cancelled"), _api_run(8, "skipped")]})
    action, _ = _plan(_run("success", number=11), gh)
    assert action.kind == "comment" and "unknown: `Docs`" in action.text


def test_a_rerun_that_succeeds_takes_the_recovery_path() -> None:
    """Attempt 2 of the run that failed: the listing now shows it as success."""
    gh = FakeGh(issues=[_issue(77)], runs={"ci.yml": [_api_run(10, "success")],
                                           "docs.yaml": [_api_run(8, "success")]})
    action, _ = _plan(_run("success", number=10, attempt=2), gh)
    assert action.kind == "close" and "run attempt 2" in action.text


def test_the_newest_run_decides_not_the_run_that_triggered() -> None:
    """A re-run of an OLDER run succeeds while a newer run of the same workflow
    is red: the issue stays open."""
    gh = FakeGh(issues=[_issue(77)], runs={
        "ci.yml": [_api_run(12, "failure"), _api_run(10, "success")],
        "docs.yaml": [_api_run(8, "success")]})
    action, _ = _plan(_run("success", number=10, attempt=2), gh)
    assert action.kind == "comment" and "still red: `CI`" in action.text


# ── GitHub does not answer ──────────────────────────────────────────────────

@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_a_failed_issue_lookup_is_never_no_issue(conclusion) -> None:
    """⛔ The cell this module exists for: reading a failed lookup as "none
    open" opens a duplicate issue on every failing run."""
    gh = FakeGh(issues=[_issue(77)], runs=GREEN, fail=("issues",))
    with pytest.raises(mrr.NoVerdict, match="tracking issue"):
        _plan(_run(conclusion), gh)
    assert gh.written == []


def test_a_failed_runs_listing_is_never_green() -> None:
    gh = FakeGh(issues=[_issue(77)], runs=GREEN, fail=("actions/workflows/docs.yaml",))
    with pytest.raises(mrr.NoVerdict, match="green again"):
        _plan(_run("success"), gh)
    assert gh.written == []


@pytest.mark.parametrize("answer", [{"message": "Bad credentials"}, [None], [{"title": "x"}]],
                         ids=["object", "null-entry", "no-number"])
def test_an_issue_list_in_another_shape_is_refused(answer) -> None:
    api = mrr.Api(REPO, runner=lambda a, s: subprocess.CompletedProcess(a, 0, json.dumps(answer), ""))
    with pytest.raises(mrr.ApiError):
        mrr.find_issue(api)


def test_an_answer_that_is_not_json_is_refused() -> None:
    api = mrr.Api(REPO, runner=lambda a, s: subprocess.CompletedProcess(a, 0, "<html>", ""))
    with pytest.raises(mrr.ApiError, match="not JSON"):
        mrr.find_issue(api)


def test_gh_missing_is_an_api_error_not_a_crash() -> None:
    def runner(args, stdin):
        raise FileNotFoundError("gh")
    with pytest.raises(mrr.ApiError, match="could not run"):
        mrr.find_issue(mrr.Api(REPO, runner=runner))


def test_what_only_describes_the_failure_does_not_stop_the_report() -> None:
    """The jobs, the range start and the commit list are description. Losing
    them must not lose the issue — and each gap is stated, not papered over."""
    gh = FakeGh(runs={"ci.yml": [_api_run(8, "success", sha="a" * 40)]},
                fail=("actions/runs/", "compare/"))
    action, _ = _plan(_run(), gh)
    assert action.kind == "open"
    assert "the job list could not be read" in action.text
    assert "The commits in the range could not be read" in action.text

    gh = FakeGh(fail=("actions/workflows/",))
    action, _ = _plan(_run(), gh)
    assert action.kind == "open"
    assert "Range: start unknown .. `fffffffff`" in action.text
    assert "the runs listing could not be read" in action.text


def test_a_range_start_that_cannot_be_found_is_said_not_invented() -> None:
    gh = FakeGh(runs={"ci.yml": [_api_run(11, "success"),      # newer: not a start
                                 _api_run(10, "failure"), _api_run(9, "cancelled"),
                                 _api_run(8, "failure")]})
    action, _ = _plan(_run(number=10), gh)
    assert "Range: start unknown .. `fffffffff`" in action.text
    assert "none of the newest 4 push runs" in action.text
    assert "Commits in the range" not in action.text
    assert not [c for c in gh.calls if c[1].startswith("compare/")]


def test_a_failure_on_the_commit_that_last_passed_is_called_flaky() -> None:
    gh = FakeGh(runs={"ci.yml": [_api_run(9, "success", sha="f" * 40)]})
    action, _ = _plan(_run(number=10), gh)
    assert "nothing was merged in between" in action.text
    assert not [c for c in gh.calls if c[1].startswith("compare/")]


def test_a_long_range_is_cut_and_says_so() -> None:
    commits = [{"sha": f"{i:040x}", "commit": {"message": f"c{i}"}} for i in range(100)]
    gh = FakeGh(runs={"ci.yml": [_api_run(8, "success", sha="a" * 40)]},
                compare={"total_commits": 400, "commits": commits})
    action, _ = _plan(_run(), gh)
    assert f"(400; the newest {mrr.MAX_COMMITS_SHOWN} shown, oldest first)" in action.text
    assert " c99" in action.text and " c69\n" not in action.text


def test_a_failed_run_whose_jobs_all_passed_is_said() -> None:
    gh = FakeGh(runs={"ci.yml": []}, jobs=[{"name": "a", "conclusion": "success"}])
    action, _ = _plan(_run("startup_failure"), gh)
    assert "no job of this attempt reported a failing conclusion" in action.text


# ── finding the issue ───────────────────────────────────────────────────────

@pytest.mark.parametrize("issues, expected", [
    ([], None),
    ([_issue(5, "x " + mrr.ISSUE_TITLE)], None),
    ([_issue(5, mrr.ISSUE_TITLE + " (again)")], None),
    ([_issue(5, mrr.ISSUE_TITLE.upper())], None),
    ([_issue(5, pull_request={})], None),
    ([_issue(9), _issue(5)], 5),
], ids=["none", "prefixed", "suffixed", "case", "a-pull-request", "oldest-of-two"])
def test_the_issue_is_matched_by_exact_title(issues, expected) -> None:
    found = mrr.find_issue(mrr.Api(REPO, runner=FakeGh(issues=issues)))
    assert (found["number"] if found else None) == expected


def test_the_issue_is_found_on_a_later_page() -> None:
    others = [_issue(1000 + i, f"other {i}") for i in range(mrr.PAGE + 20)]
    gh = FakeGh(issues=[*others, _issue(77)])
    assert mrr.find_issue(mrr.Api(REPO, runner=gh))["number"] == 77
    assert [c for c in gh.calls] == [("GET", "issues")] * 2


def test_an_issue_list_that_never_ends_is_refused_not_cut_short(monkeypatch) -> None:
    monkeypatch.setattr(mrr, "MAX_ISSUE_PAGES", 2)
    others = [_issue(1000 + i, f"other {i}") for i in range(mrr.PAGE * 2)]
    with pytest.raises(mrr.ApiError, match="not read to its end"):
        mrr.find_issue(mrr.Api(REPO, runner=FakeGh(issues=[*others, _issue(77)])))


# ── inputs ──────────────────────────────────────────────────────────────────

_PAYLOAD = {"name": "CI", "id": 5010, "run_attempt": 1, "run_number": 10, "event": "push",
            "head_branch": "main", "head_sha": "f" * 40, "conclusion": "failure",
            "html_url": "https://example.invalid/runs/5010"}


def test_the_event_payload_is_read() -> None:
    assert mrr.run_from(_PAYLOAD) == _run()


@pytest.mark.parametrize("patch", [
    {"conclusion": None}, {"conclusion": ""}, {"id": None}, {"id": "5010"}, {"id": True},
    {"head_sha": ""}, {"name": None}, {"event": None}, {"head_branch": None},
    {"run_attempt": None},
], ids=lambda p: "-".join(f"{k}={v!r}" for k, v in p.items()))
def test_a_payload_with_a_missing_field_is_refused_not_defaulted(patch) -> None:
    with pytest.raises(mrr.NoVerdict):
        mrr.run_from({**_PAYLOAD, **patch})


def test_an_unwatched_workflow_is_refused() -> None:
    """The trigger list and the event disagree: nothing can be concluded."""
    with pytest.raises(mrr.NoVerdict, match="not one of the watched"):
        _plan(_run(workflow="Elsewhere"), FakeGh())


def _tree(tmp_path: Path, detector_names, files: dict[str, str]) -> tuple[Path, Path]:
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    for name, text in files.items():
        (wf_dir / name).write_text(text, encoding="utf-8")
    detector = wf_dir / "detector.yml"
    detector.write_text(yaml.safe_dump({"name": "Detector", "on": {
        "workflow_run": {"workflows": detector_names}}}), encoding="utf-8")
    return detector, wf_dir


def test_the_watched_list_is_resolved_to_files_and_push_triggers(tmp_path) -> None:
    detector, wf_dir = _tree(tmp_path, ["A", "B", "C", "D"], {
        "a.yml": "name: A\non:\n  push:\n    branches: [main]\n  pull_request:\n",
        "b.yaml": "name: B\non:\n  pull_request:\n",
        "c.yml": "name: C\non: [push, pull_request]\n",
        "d.yml": "name: D\non:\n  push:\n    branches: [release]\n"})
    assert mrr.watched_workflows(detector, wf_dir) == [
        mrr.Watched("A", "a.yml", True), mrr.Watched("B", "b.yaml", False),
        mrr.Watched("C", "c.yml", True), mrr.Watched("D", "d.yml", False)]


@pytest.mark.parametrize("names, files", [
    (["A"], {}),
    (["A"], {"a.yml": "name: A\non: push\n", "b.yml": "name: A\non: push\n"}),
    ([], {"a.yml": "name: A\non: push\n"}),
    ("A", {"a.yml": "name: A\non: push\n"}),
], ids=["no-such-workflow", "two-with-the-name", "empty", "not-a-list"])
def test_a_watched_list_that_does_not_resolve_is_refused(tmp_path, names, files) -> None:
    detector, wf_dir = _tree(tmp_path, names, files)
    with pytest.raises(mrr.NoVerdict):
        mrr.watched_workflows(detector, wf_dir)


@pytest.mark.parametrize("text", ["", "name: D\n", "name: D\non: push\n", "on: [\n"],
                         ids=["empty", "no-on", "no-workflow-run", "not-yaml"])
def test_a_detector_file_without_the_list_is_refused(tmp_path, text) -> None:
    detector, wf_dir = _tree(tmp_path, ["A"], {"a.yml": "name: A\non: push\n"})
    detector.write_text(text, encoding="utf-8")
    with pytest.raises(mrr.NoVerdict):
        mrr.watched_workflows(detector, wf_dir)
    with pytest.raises(mrr.NoVerdict):
        mrr.watched_workflows(tmp_path / "absent.yml", wf_dir)


# ── exit codes (through main(), against the repo's own workflow list) ───────

def _event(tmp_path: Path, **patch) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"workflow_run": {**_PAYLOAD, **patch}}), encoding="utf-8")
    return str(path)


def _repo_runs(conclusion: str) -> dict:
    return {wf.file: [_api_run(3, conclusion)] for wf in mrr.watched_workflows()}


def _main(tmp_path, gh, *extra, **patch) -> int:
    return mrr.main(["--repo", REPO, "--event-file", _event(tmp_path, **patch), *extra],
                    runner=gh)


WRITE = ("--write",)


def test_exit_codes(tmp_path, capsys) -> None:
    """0 nothing to report (including the close) · 1 main is red, the issue
    carries it · 2 no verdict."""
    green, red = _repo_runs("success"), _repo_runs("failure")
    cases = [
        ("open",          FakeGh(runs=red), {}, 1, 1),
        ("comment",       FakeGh(issues=[_issue()], runs=red), {}, 1, 1),
        ("still-red",     FakeGh(issues=[_issue()], runs=red), {"conclusion": "success"}, 1, 1),
        ("close",         FakeGh(issues=[_issue()], runs=green), {"conclusion": "success"}, 0, 2),
        ("green-no-issue", FakeGh(runs=green), {"conclusion": "success"}, 0, 0),
        ("cancelled",     FakeGh(runs=red), {"conclusion": "cancelled"}, 0, 0),
        ("pull-request",  FakeGh(runs=red), {"event": "pull_request"}, 0, 0),
        ("lookup-failed", FakeGh(runs=red, fail=("issues",)), {}, 2, 0),
        ("listing-failed", FakeGh(issues=[_issue()], runs=green, fail=("actions/workflows/",)),
         {"conclusion": "success"}, 2, 0),
        ("no-conclusion", FakeGh(runs=red), {"conclusion": None}, 2, 0),
    ]
    for label, gh, patch, rc, writes in cases:
        assert _main(tmp_path, gh, *WRITE, **patch) == rc, label
        assert len(gh.written) == writes, (label, gh.written)
    capsys.readouterr()


def test_a_write_that_fails_is_exit_2(tmp_path, capsys) -> None:
    """The alert did not land: never 0, never the 'main is red' 1."""
    class WriteFails(FakeGh):
        def __call__(self, args, stdin):
            if args[2] != "GET":
                return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 403")
            return super().__call__(args, stdin)
    assert _main(tmp_path, WriteFails(runs=_repo_runs("failure")), *WRITE) == 2
    assert "no verdict" in capsys.readouterr().err


def test_a_comment_that_lands_and_a_close_that_fails_is_exit_2(tmp_path, capsys) -> None:
    class CloseFails(FakeGh):
        def __call__(self, args, stdin):
            if args[2] == "PATCH":
                return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 403")
            return super().__call__(args, stdin)
    gh = CloseFails(issues=[_issue()], runs=_repo_runs("success"))
    assert _main(tmp_path, gh, *WRITE, conclusion="success") == 2
    capsys.readouterr()


def test_without_write_everything_is_read_and_nothing_is_written(tmp_path, capsys) -> None:
    """Read-only is the default, not a flag someone has to remember; the exit
    code is the one the write would have had."""
    gh = FakeGh(runs=_repo_runs("failure"))
    assert _main(tmp_path, gh) == 1
    assert gh.written == []
    assert [m for m, _ in gh.calls if m != "GET"] == []
    out = capsys.readouterr().out
    assert "DRY RUN — would POST issues" in out and mrr.ISSUE_TITLE in out


def test_a_run_id_is_read_from_the_api(capsys) -> None:
    gh = FakeGh(run=_PAYLOAD, runs=_repo_runs("failure"))
    assert mrr.main(["--repo", REPO, "--run-id", "5010"], runner=gh) == 1
    assert gh.calls[0] == ("GET", "actions/runs/5010")
    capsys.readouterr()


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"workflow_run": 3}'],
                         ids=["not-json", "a-list", "no-workflow-run", "not-an-object"])
def test_an_event_file_that_cannot_be_read_is_exit_2(tmp_path, content, capsys) -> None:
    path = tmp_path / "event.json"
    path.write_text(content, encoding="utf-8")
    gh = FakeGh()
    assert mrr.main(["--repo", REPO, "--event-file", str(path)], runner=gh) == 2
    assert mrr.main(["--repo", REPO, "--event-file", str(tmp_path / "absent")], runner=gh) == 2
    assert gh.calls == []
    capsys.readouterr()


def test_the_script_exits_through_run() -> None:
    """As a process, through `_lib_exitcodes.run`: a usage error is 2 and
    prints no traceback."""
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, check=False)
    assert proc.returncode == 2, proc.stderr
    assert "Traceback" not in proc.stderr and "--repo" in proc.stderr, proc.stderr


# ── the workflow ────────────────────────────────────────────────────────────

def _detector() -> dict:
    return yaml.safe_load(DETECTOR.read_text(encoding="utf-8"))


def _on(doc: dict):
    return doc.get("on", doc.get(True))


@pytest.fixture(scope="module")
def agg():
    """The aggregate-gate contract module: it owns the derivation of "the
    workflows that report a required check" from .github/required-checks.yaml."""
    return _load("agg_contract_for_main_red",
                 ROOT / "tests/ops/test_ci_aggregate_gate_contract.py")


def watch_list_problems(listed: list[str], derived: dict[str, str]) -> list[str]:
    """PURE. `derived`: workflow `name:` → file, for every workflow that
    reports a required check."""
    problems = [f"{name!r} is listed twice" for name in sorted(set(listed))
                if listed.count(name) > 1]
    problems += [f"{derived[name]} ({name!r}) reports a required check and is not "
                 "watched — its failures on main would open no issue"
                 for name in sorted(set(derived) - set(listed))]
    problems += [f"{name!r} is watched and reports no required check — remove it, or "
                 "register its check in .github/required-checks.yaml"
                 for name in sorted(set(listed) - set(derived))]
    return problems


def test_the_watched_workflows_are_the_ones_that_report_a_required_check(agg) -> None:
    paths = agg._required_check_workflows()
    assert len(paths) >= 3, f"only {[p.name for p in paths]} derived — the set looks emptied"
    derived = {agg._load(p)["name"]: p.name for p in paths}
    assert len(derived) == len(paths), "two required-check workflows share a `name:`"
    listed = _on(_detector())["workflow_run"]["workflows"]
    problems = watch_list_problems(listed, derived)
    assert not problems, "\n  ".join(["main-red-detector.yml `on.workflow_run.workflows`:"] + problems)
    # And the script resolves the same list to the same files.
    assert {w.name: w.file for w in mrr.watched_workflows()} == derived


@pytest.mark.parametrize("listed, expected", [
    (["A", "B"], 0), (["A"], 1), (["A", "B", "C"], 1), (["A", "A", "B"], 1), ([], 2),
], ids=["equal", "one-missing", "one-extra", "duplicate", "empty"])
def test_the_watch_list_rule_can_fail(listed, expected) -> None:
    assert len(watch_list_problems(listed, {"A": "a.yml", "B": "b.yml"})) == expected


# Watched workflows that have no `push` trigger for main, and why that is
# right. Checked both ways: a workflow here that gains the trigger, or leaves
# the watch list, must leave this table too.
_NO_MAIN_PUSH = {
    "commitlint.yaml": "lints a pull request's title and commits; a push to main "
                       "has neither, and the squash title was linted on the PR",
}


def test_every_watched_workflow_runs_on_push_to_main_or_says_why_not() -> None:
    """`.github/workflows/validate.yaml` had no push trigger until #3922: its
    three required checks never ran on the merged tree."""
    without = {w.file for w in mrr.watched_workflows() if not w.runs_on_main_push}
    assert without == set(_NO_MAIN_PUSH), (
        f"watched workflows without `on.push` for main: {sorted(without)}; "
        f"accounted for: {sorted(_NO_MAIN_PUSH)}. Such a workflow's required checks "
        "never run on the merged tree, and the detector can never see them fail.")


def test_the_trigger_is_completed_runs_on_main() -> None:
    trigger = _on(_detector())["workflow_run"]
    assert trigger["types"] == ["completed"] and trigger["branches"] == ["main"], trigger
    assert set(_on(_detector())) == {"workflow_run", "workflow_dispatch"}


def test_permissions_are_the_three_the_script_needs() -> None:
    doc = _detector()
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == ["report"]
    assert doc["jobs"]["report"]["permissions"] == {
        "contents": "read", "actions": "read", "issues": "write"}


def test_runs_are_serialised_and_never_cancelled_midway() -> None:
    """Two workflows failing on one merge must not both find 'no issue'."""
    assert _detector()["concurrency"] == {"group": "main-red-detector",
                                          "cancel-in-progress": False}


def test_nothing_from_the_triggering_run_is_checked_out_or_interpolated() -> None:
    """`workflow_run` holds a write token. The checkout names the default
    branch's sha, and no step puts event data into a script."""
    steps = _detector()["jobs"]["report"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
    for step in steps:
        assert "${{" not in str(step.get("run", "")), step.get("name")
        assert "workflow_run" not in json.dumps(step), step.get("name")


def test_the_detector_is_not_part_of_a_required_check_workflow(agg) -> None:
    """A job in ci.yml / docs-ci.yaml / validate.yaml would have to sit in a
    registered check's `needs` closure with no `if:`; and nothing here may be
    mistaken for a required or advisory check."""
    assert DETECTOR not in agg._required_check_workflows()
    names = {str(job.get("name", job_id)) for job_id, job in _detector()["jobs"].items()}
    assert not names & (agg._ALL_REQUIRED_NAMES | agg._ADVISORY_NAMES), names


def test_only_no_verdict_fails_the_workflow_run() -> None:
    """rc 1 (main is red, the issue carries it) must leave the detector green:
    a red detector means the detector is broken. Executed, not read."""
    import shutil
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash")
    run = _detector()["jobs"]["report"]["steps"][-1]["run"]
    tail = run[run.index('case "$rc" in'):]
    for rc, expected in ((0, 0), (1, 0), (2, 2), (3, 2), (120, 2)):
        proc = subprocess.run([bash, "-c", f"rc={rc}\n{tail}"], capture_output=True,
                              text=True, encoding="utf-8", timeout=30, check=False)
        assert proc.returncode == expected, (rc, proc.stdout, proc.stderr)


@pytest.mark.parametrize("env, expected_rc, expected_args", [
    ({"GITHUB_EVENT_NAME": "workflow_run", "RUN_ID": "", "WRITE": ""}, 0,
     "--repo o/r --event-file /ev.json --write"),
    ({"GITHUB_EVENT_NAME": "workflow_dispatch", "RUN_ID": "77", "WRITE": "false"}, 0,
     "--repo o/r --run-id 77"),
    ({"GITHUB_EVENT_NAME": "workflow_dispatch", "RUN_ID": "77", "WRITE": "true"}, 0,
     "--repo o/r --run-id 77 --write"),
    ({"GITHUB_EVENT_NAME": "workflow_dispatch", "RUN_ID": "7; id", "WRITE": "true"}, 2, None),
    ({"GITHUB_EVENT_NAME": "workflow_dispatch", "RUN_ID": "", "WRITE": "true"}, 2, None),
], ids=["event-writes", "replay-reads", "replay-writes-when-asked", "bad-run-id", "no-run-id"])
def test_the_step_hands_the_script_the_right_arguments(tmp_path, env, expected_rc,
                                                       expected_args) -> None:
    """The script is read-only unless given `--write`, so the event path must
    pass it — a detector that never writes is green and silent. Executed with
    a stand-in `python3` that records its arguments."""
    import os
    import shutil
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("needs bash")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "python3"
    fake.write_text('#!/bin/sh\nshift 3\necho "ARGS: $*"\n', encoding="utf-8")
    fake.chmod(0o755)
    run = _detector()["jobs"]["report"]["steps"][-1]["run"]
    assert "python3 -X utf8 scripts/ops/main_red_report.py" in run
    proc = subprocess.run(
        [bash, "-c", run], capture_output=True, text=True, encoding="utf-8", timeout=30,
        check=False, cwd=tmp_path,
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path),
             "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
             "GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_PATH": "/ev.json", **env})
    assert proc.returncode == expected_rc, (proc.stdout, proc.stderr)
    if expected_args is None:
        assert "ARGS:" not in proc.stdout, proc.stdout
    else:
        assert f"ARGS: {expected_args}\n" in proc.stdout, proc.stdout
