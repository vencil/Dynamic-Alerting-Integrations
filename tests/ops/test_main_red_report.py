"""The red-main detector (#3922): `scripts/ops/main_red_report.py` and
`.github/workflows/main-red-detector.yml`.

What is held here:

  * the script's decision table (its docstring states it once; these are its
    rows), driven through a fake `gh` so no test touches the network. The
    decision follows main's STATE, not the event, so most rows set up runs
    listings and an issue and then send an unrelated event.
    ⛔ The rows that matter most are the ones where GitHub does not answer: a
    failed issue lookup must never read as "no issue is open" (a duplicate
    per run) and a failed runs listing must never read as "green" (the issue
    closes while main is red). Next: an issue someone else opened under the
    same title is never the tracking issue;
  * the workflow's shape: its `workflow_run.workflows` equals the set of
    workflows that report an entry of `.github/required-checks.yaml` (derived
    by tests/ops/test_ci_aggregate_gate_contract.py, not re-derived here), its
    permissions and token, and that it checks out nothing from the triggering
    run;
  * that every watched workflow has a `push` trigger for main, except the ones
    named in `_NO_MAIN_PUSH` with a reason — `.github/workflows/validate.yaml`
    had none until this change, so its required checks never ran on the
    merged tree.

⚠️ Not covered: the workflow running on GitHub. `workflow_run` fires only from
the default branch, so no PR can exercise it; the `workflow_dispatch` replay
(`run_id`, read-only by default) is the post-merge check.
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

BOT = {"login": "github-actions[bot]", "type": "Bot"}
MALLORY = {"login": "mallory", "type": "User"}


# ── a fake `gh api` ─────────────────────────────────────────────────────────

class FakeGh:
    """Answers `gh api` argv from tables. `fail` holds path prefixes that exit
    1, the way `gh` does on a 5xx/403.

    ⛔ The issue list is NOT filtered by `creator` here: the script must not
    rely on the server having filtered, so the fake behaves like a server
    that did not."""

    def __init__(self, *, issues=(), comments=None, runs=None, jobs=None, commits=None,
                 total=None, run=None, fail=()):
        self.issues = list(issues)
        self.comments = comments or {}  # issue number → [{user, body}]
        self.runs = runs or {}          # workflow file → list of runs
        self.jobs = jobs if jobs is not None else []
        self.commits = commits or []    # the whole range, oldest first
        self.total = total              # total_commits, when it should differ
        self.run = run
        self.fail = tuple(fail)
        self.calls: list[tuple[str, str]] = []
        self.params: list[dict] = []
        self.written: list[tuple[str, str, dict]] = []

    def __call__(self, args, stdin):
        assert args[0] == "api" and args[1] == "-X", args
        method, path = args[2], args[3].removeprefix(f"repos/{REPO}/")
        params = dict(a.split("=", 1) for a in args[5::2]) if method == "GET" else {}
        self.calls.append((method, path))
        self.params.append(params)
        if any(path.startswith(prefix) for prefix in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "gh: HTTP 502")
        if method != "GET":
            assert args[4:] == ["--input", "-"], args
            self.written.append((method, path, json.loads(stdin)))
            return self._ok({"number": 900})
        page, per = int(params.get("page", 1)), int(params.get("per_page", 30))
        if path == "issues":
            assert params["state"] == "open", params
            assert params["creator"] == "github-actions[bot]", params
            return self._ok(self.issues[(page - 1) * per: page * per])
        if path.startswith("issues/") and path.endswith("/comments"):
            rows = self.comments.get(int(path.split("/")[1]), [])
            return self._ok(rows[(page - 1) * per: page * per])
        if path.startswith("actions/workflows/"):
            assert params["branch"] == "main" and params["event"] == "push", params
            return self._ok({"workflow_runs": self.runs[path.split("/")[2]]})
        if path.startswith("actions/runs/") and path.endswith("/jobs"):
            return self._ok({"jobs": self.jobs if page == 1 else []})
        if path.startswith("actions/runs/"):
            return self._ok(self.run)
        if path.startswith("compare/"):
            return self._ok(self._compare(page, per))
        raise AssertionError(f"unexpected call {args}")

    def _compare(self, page, per):
        return {"total_commits": len(self.commits) if self.total is None else self.total,
                "commits": self.commits[(page - 1) * per: page * per]}

    @staticmethod
    def _ok(doc):
        return subprocess.CompletedProcess([], 0, json.dumps(doc), "")

    def writes(self):
        return [(m, p) for m, p, _ in self.written]


WATCHED = [mrr.Watched("CI", "ci.yml", True),
           mrr.Watched("Docs", "docs.yaml", True),
           mrr.Watched("PR only", "pr.yaml", False)]


def _sha(number: int) -> str:
    return f"{number:040x}"


def R(number, conclusion, *, status="completed", attempt=1, sha=None, name="CI"):
    """A run as a runs listing (and the event payload) carries it."""
    return {"name": name, "id": 5000 + number, "run_number": number, "run_attempt": attempt,
            "event": "push", "head_branch": "main", "status": status,
            "conclusion": conclusion, "head_sha": sha or _sha(number),
            "html_url": f"https://example.invalid/runs/{5000 + number}"}


def _event(conclusion="success", *, workflow="Docs", number=3, attempt=1, event="push",
           branch="main"):
    """The run whose completion woke the detector. By default one that says
    nothing about CI — the decision must come from the state."""
    return mrr.Run(workflow, 5000 + number, attempt, number, event, branch, _sha(number),
                   conclusion, f"https://example.invalid/runs/{5000 + number}")


def _issue(number=77, title=None, *, user=BOT, body="", **extra):
    return {"number": number, "title": mrr.ISSUE_TITLE if title is None else title,
            "user": user, "body": body, **extra}


def _mark(colour, file, number, attempt=1):
    return f"<!-- main-red: {colour} {file} {5000 + number} {attempt} -->"


DOCS_GREEN = [R(3, "success", name="Docs")]
DOCS_RED = [R(4, "failure", name="Docs"), R(3, "success", name="Docs")]


def _gh(ci, docs=DOCS_GREEN, **kw):
    return FakeGh(runs={"ci.yml": ci, "docs.yaml": docs}, **kw)


def _plan(gh, event=None, watched=WATCHED):
    api = mrr.Api(REPO, runner=gh)
    action = mrr.plan(event or _event(), watched, api)
    mrr.apply(action, api)
    return action


# ── some workflow is red ────────────────────────────────────────────────────

def test_red_with_no_issue_opens_one_whatever_the_event_was() -> None:
    """The event is a green Docs run; CI's newest verdict is a failure and no
    issue is open — a failure event that was never delivered. The state, not
    the event, opens the issue, and the report is about CI's failing run."""
    gh = _gh([R(10, "failure"), R(9, "cancelled"), R(8, "success")],
             jobs=[{"name": "Python Tests", "conclusion": "failure"},
                   {"name": "Go Tests", "conclusion": "success"},
                   {"name": "Lint", "conclusion": "skipped"},
                   {"name": "Slow", "conclusion": "timed_out"}],
             commits=[{"sha": "b" * 40, "commit": {"message": "feat: one\n\nbody"}},
                      {"sha": _sha(10), "commit": {"message": "fix: two @someone #12 ```"}}])
    action = _plan(gh)
    assert (action.kind, action.issue, action.red) == ("open", None, True)
    text = action.text
    assert _mark("red", "ci.yml", 10) in text.splitlines()
    assert f"`{_sha(8)[:9]}..{_sha(10)[:9]}`" in text, text
    assert "https://example.invalid/runs/5010" in text
    assert "`Python Tests` (failure)" in text and "`Slow` (timed_out)" in text
    assert "Go Tests" not in text and "`Lint`" not in text
    assert "bbbbbbbbb feat: one" in text and "body" not in text.split("```")[1]
    # Untrusted text stays inside the fence: the subject cannot close it.
    assert text.count("```") == 2, text
    assert gh.written == [("POST", "issues", {"title": mrr.ISSUE_TITLE, "body": text})]
    # The per-attempt endpoint: a re-run started meanwhile must not replace
    # the jobs this report is about.
    assert ("GET", "actions/runs/5010/attempts/1/jobs") in gh.calls


def test_two_red_workflows_share_one_issue() -> None:
    gh = _gh([R(10, "failure")], DOCS_RED)
    action = _plan(gh)
    assert action.kind == "open" and gh.writes() == [("POST", "issues")]
    lines = action.text.splitlines()
    assert _mark("red", "ci.yml", 10) in lines and _mark("red", "docs.yaml", 4) in lines
    assert mrr.ISSUE_TITLE.count("CI") == 0, "the title names no workflow"


def test_red_already_recorded_writes_nothing() -> None:
    """Every further event while CI stays red on the same run: no comment.
    Still exit 1 — this run looked, and main is red."""
    gh = _gh([R(10, "failure")], issues=[_issue(body="x\n" + _mark("red", "ci.yml", 10))])
    action = _plan(gh)
    assert (action.kind, action.issue, action.red) == ("noop", 77, True)
    assert gh.written == []


@pytest.mark.parametrize("body, comments", [
    (_mark("red", "ci.yml", 9), []),
    (_mark("red", "ci.yml", 10, attempt=1), []),
    (_mark("red", "docs.yaml", 10), []),
    ("", []),
    (_mark("red", "ci.yml", 10, attempt=2),
     [{"user": BOT, "body": _mark("green", "ci.yml", 11)}]),
], ids=["older-run", "earlier-attempt", "another-workflow", "no-marker", "green-since"])
def test_a_red_run_the_issue_has_not_recorded_is_commented(body, comments) -> None:
    gh = _gh([R(10, "failure", attempt=2), R(8, "success")],
             issues=[_issue(body=body)], comments={77: comments})
    action = _plan(gh)
    assert (action.kind, action.issue, action.red) == ("comment", 77, True)
    assert _mark("red", "ci.yml", 10, attempt=2) in action.text.splitlines()
    assert "attempt 2" in action.text
    assert gh.writes() == [("POST", "issues/77/comments")]


def test_the_last_marker_of_a_workflow_wins_across_body_and_comments() -> None:
    gh = _gh([R(12, "failure")], issues=[_issue(body=_mark("red", "ci.yml", 10))],
             comments={77: [{"user": BOT, "body": "a\r\n" + _mark("red", "ci.yml", 11) + "\r\nb"},
                            {"user": BOT, "body": _mark("red", "ci.yml", 12)}]})
    assert _plan(gh).kind == "noop" and gh.written == []


def test_a_marker_in_a_crlf_body_is_read() -> None:
    """Bodies come back as they were sent; an edit in the web UI makes them CRLF."""
    gh = _gh([R(10, "failure")],
             issues=[_issue(body="intro\r\n" + _mark("red", "ci.yml", 10) + "\r\nrest\r\n")])
    assert _plan(gh).kind == "noop" and gh.written == []


def test_a_partial_recovery_is_said_once_and_does_not_close() -> None:
    """CI was recorded red and is green now; Docs is still red."""
    body = _mark("red", "ci.yml", 10) + "\n" + _mark("red", "docs.yaml", 4)
    gh = _gh([R(11, "success"), R(10, "failure")], DOCS_RED, issues=[_issue(body=body)])
    action = _plan(gh)
    assert (action.kind, action.red) == ("comment", True)
    assert _mark("green", "ci.yml", 11) in action.text.splitlines()
    assert "**CI** is green again" in action.text
    assert "still red: `Docs`" in action.text and "Not closing" in action.text
    assert gh.writes() == [("POST", "issues/77/comments")]
    # …and the next event finds the recovery recorded: nothing more to say.
    gh2 = _gh([R(11, "success"), R(10, "failure")], DOCS_RED, issues=[_issue(body=body)],
              comments={77: [{"user": BOT, "body": action.text}]})
    assert _plan(gh2).kind == "noop" and gh2.written == []


def test_a_new_failure_and_a_recovery_go_into_one_comment() -> None:
    gh = _gh([R(11, "success"), R(10, "failure")], DOCS_RED,
             issues=[_issue(body=_mark("red", "ci.yml", 10))])
    action = _plan(gh)
    lines = action.text.splitlines()
    assert _mark("green", "ci.yml", 11) in lines and _mark("red", "docs.yaml", 4) in lines
    assert "still red" not in action.text, "Docs is reported in full, not as 'still'"


def test_no_comment_claims_a_recovery_while_that_workflow_is_red() -> None:
    """A re-run of OLD run 10 succeeds while newer run 12 of the same workflow
    is red: the state is red on 12. Nothing says CI recovered."""
    gh = _gh([R(12, "failure"), R(10, "success", attempt=2)],
             issues=[_issue(body=_mark("red", "ci.yml", 12))])
    action = _plan(gh, _event("success", workflow="CI", number=10, attempt=2))
    assert action.kind == "noop" and gh.written == []
    gh = _gh([R(12, "failure"), R(10, "success", attempt=2)],
             issues=[_issue(body=_mark("red", "ci.yml", 10))])
    action = _plan(gh, _event("success", workflow="CI", number=10, attempt=2))
    assert action.kind == "comment" and "green again" not in action.text
    assert _mark("red", "ci.yml", 12) in action.text.splitlines()


# ── nothing is red ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", ["", _mark("red", "ci.yml", 10)], ids=["bare", "recorded"])
def test_all_green_closes_the_issue(body) -> None:
    gh = _gh([R(13, None, status="in_progress"), R(12, "cancelled"), R(11, "success"),
              R(10, "failure")], issues=[_issue(body=body)])
    action = _plan(gh)
    assert (action.kind, action.issue, action.red) == ("close", 77, False)
    assert "green again" in action.text
    assert gh.written[0][:2] == ("POST", "issues/77/comments")
    assert gh.written[1] == ("PATCH", "issues/77",
                             {"state": "closed", "state_reason": "completed"})


def test_all_green_and_no_issue_does_nothing() -> None:
    gh = _gh([R(11, "success")])
    action = _plan(gh)
    assert (action.kind, action.red) == ("noop", False) and gh.written == []


def test_a_failure_event_for_a_run_since_rerun_to_success_opens_nothing() -> None:
    """The event says attempt 1 failed; the listing already shows the same run
    at attempt 2, success. The latest attempt is the run's verdict."""
    gh = _gh([R(10, "success", attempt=2), R(9, "success")])
    action = _plan(gh, _event("failure", workflow="CI", number=10))
    assert (action.kind, action.red) == ("noop", False) and gh.written == []


def test_an_old_run_rerun_into_a_failure_changes_nothing() -> None:
    gh = _gh([R(11, "success"), R(10, "failure", attempt=2)])
    action = _plan(gh, _event("failure", workflow="CI", number=10, attempt=2))
    assert (action.kind, action.red) == ("noop", False) and gh.written == []


@pytest.mark.parametrize("docs, why", [
    ([R(9, "cancelled", name="Docs"), R(8, "skipped", name="Docs")], "none of its newest 2"),
    ([], "none of its newest 0"),
    ([R(9, None, status="in_progress", name="Docs")], "none of its newest 1"),
    ([R(9, None, status="in_progress", attempt=2, name="Docs"), R(8, "success", name="Docs")],
     "run #9 is being re-run"),
    ([R(9, "cancelled", attempt=2, name="Docs"), R(8, "success", name="Docs")],
     "run #9 is being re-run"),
], ids=["all-cancelled", "never-ran", "first-run-going", "rerun-going", "rerun-cancelled"])
def test_a_workflow_without_a_verdict_is_unknown_not_green(docs, why) -> None:
    """Unknown never closes the issue, and never opens one: "has not finished a
    push run yet" is not "red" and not "unreadable". A re-run in progress hides
    the verdict of its earlier attempt — the run below it is not main's state."""
    gh = _gh([R(11, "success")], docs, issues=[_issue(body=_mark("red", "ci.yml", 10))])
    action = _plan(gh)
    assert (action.kind, action.red) == ("comment", True)      # CI's recovery, once
    assert f"unknown: `Docs` — {why}" in action.text and "PATCH" not in str(gh.writes())
    gh = _gh([R(11, "success")], docs, issues=[_issue()])
    action = _plan(gh)
    assert (action.kind, action.issue, action.red) == ("noop", 77, True) and gh.written == []
    gh = _gh([R(11, "success")], docs)
    action = _plan(gh)
    assert (action.kind, action.red) == ("noop", False) and gh.written == []


# ── which run is "newest", and what is a verdict ────────────────────────────

def test_newest_is_the_highest_run_number_not_the_order_of_the_listing() -> None:
    """Runs finish out of order (a re-run of an old run finishes after its
    successors; measured on main: CI #8575 completed after #8580). Neither
    the listing's order nor completion time decides — `run_number` does."""
    gh = _gh([R(10, "success"), R(12, "failure"), R(11, "success")])
    assert _plan(gh).kind == "open"
    gh = _gh([R(10, "failure"), R(12, "success"), R(11, "failure")])
    assert _plan(gh).kind == "noop"


@pytest.mark.parametrize("run, expected", [
    (R(1, "success"), True), (R(1, "failure"), True), (R(1, "timed_out"), True),
    (R(1, "cancelled"), False), (R(1, "skipped"), False),
    (R(1, None), False), (R(1, ""), False),
    (R(1, None, status="in_progress"), False), (R(1, None, status="queued"), False),
    (R(1, "success", status="in_progress"), False),
    (R(1, 7), False),
], ids=["success", "failure", "timed_out", "cancelled", "skipped", "null", "empty",
        "in-progress", "queued", "stale-conclusion-while-running", "not-a-string"])
def test_what_counts_as_a_verdict(run, expected) -> None:
    """⛔ main almost always has a CI run in progress: `conclusion: null` must
    be neither green nor red, or every event would misjudge the state."""
    assert mrr.is_verdict(run) is expected


def test_a_run_in_progress_is_passed_over_in_both_directions() -> None:
    gh = _gh([R(13, None, status="in_progress"), R(12, "failure")])
    assert _plan(gh).kind == "open"
    gh = _gh([R(13, None, status="in_progress"), R(12, "success")], issues=[_issue()])
    assert _plan(gh).kind == "close"


# ── the event only says when to look ────────────────────────────────────────

@pytest.mark.parametrize("conclusion", ["cancelled", "skipped"])
def test_an_event_without_a_verdict_reads_nothing(conclusion) -> None:
    gh = _gh([R(10, "failure")], issues=[_issue()])
    action = _plan(gh, _event(conclusion))
    assert (action.kind, action.red) == ("noop", False) and gh.calls == []


@pytest.mark.parametrize("kwargs", [
    {"event": "pull_request"}, {"event": "schedule"}, {"branch": "release"},
    {"event": "pull_request", "branch": "main"},
], ids=["pr", "schedule", "other-branch", "fork-branch-named-main"])
@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_only_push_runs_on_main_wake_it(kwargs, conclusion) -> None:
    gh = _gh([R(10, "failure")], issues=[_issue()])
    action = _plan(gh, _event(conclusion, **kwargs))
    assert action.kind == "noop" and gh.calls == []


def test_an_unwatched_workflow_is_refused() -> None:
    """The trigger list and the event disagree: nothing can be concluded."""
    with pytest.raises(mrr.NoVerdict, match="not one of the watched"):
        _plan(_gh([R(10, "failure")]), _event(workflow="Elsewhere"))


# ── whose issue it is ───────────────────────────────────────────────────────

@pytest.mark.parametrize("user", [
    MALLORY, {"login": "github-actions[bot]", "type": "User"},
    {"login": "github-actions", "type": "Bot"}, {"login": "dependabot[bot]", "type": "Bot"},
    None, "github-actions[bot]",
], ids=["outsider", "right-login-wrong-type", "near-login", "another-bot", "no-user", "string"])
def test_an_issue_someone_else_opened_under_the_title_is_not_the_tracking_issue(user) -> None:
    """⛔ The repo is public. Without the author check an outsider's issue
    swallows the report, and is then commented on and closed with this
    workflow's token. It must be as if it were not there — and never written to."""
    theirs = _issue(500, user=user, body=_mark("red", "ci.yml", 10))
    gh = _gh([R(10, "failure")], issues=[theirs])
    assert _plan(gh).kind == "open"
    assert gh.writes() == [("POST", "issues")]
    gh = _gh([R(11, "success")], issues=[theirs])
    action = _plan(gh)
    assert (action.kind, action.red) == ("noop", False) and gh.written == []
    # Ours next to theirs: ours is used, theirs is still untouched.
    gh = _gh([R(11, "success")], issues=[theirs, _issue(77)])
    assert _plan(gh).issue == 77
    assert all("500" not in path for _, path in gh.writes())


def test_markers_in_someone_elses_comment_are_not_a_record() -> None:
    """Anyone can comment on a public issue. A forged `red … 10` must not
    silence the report of run 10; a forged `green` must not either."""
    gh = _gh([R(10, "failure")], issues=[_issue()],
             comments={77: [{"user": MALLORY, "body": _mark("red", "ci.yml", 10)},
                            {"user": None, "body": _mark("red", "ci.yml", 10)}]})
    assert _plan(gh).kind == "comment"


@pytest.mark.parametrize("text", [
    "0000000aa " + _mark("red", "ci.yml", 10),
    "  - `" + _mark("red", "ci.yml", 10) + "` (failure)",
    _mark("red", "ci.yml", 10) + " trailing",
    "<!-- main-red: red ci.yml 5010 -->",
    "<!-- main-red: amber ci.yml 5010 1 -->",
    "<!-- main-red: red ../ci.yml 5010 1 -->",
], ids=["after-a-sha", "in-a-job-line", "trailing-text", "short", "other-colour", "path"])
def test_a_marker_is_a_whole_line_in_the_fixed_format(text) -> None:
    """A commit subject or job name that spells a marker sits behind a sha or
    inside a code span — never alone on a line."""
    gh = _gh([R(10, "failure")], issues=[_issue(body=text)])
    assert _plan(gh).kind == "comment"


def test_untrusted_text_cannot_start_a_line() -> None:
    """A job name with a newline followed by a marker (or a heading) is folded
    onto its own line; so is a commit subject."""
    forged = _mark("green", "ci.yml", 10)
    gh = _gh([R(10, "failure"), R(8, "success")],
             jobs=[{"name": f"build\n{forged}\n# pwned", "conclusion": "failure"}],
             commits=[{"sha": _sha(10), "commit": {"message": f"x\r{forged}\n\nbody"}}])
    text = _plan(gh).text
    assert forged not in text.splitlines() and "# pwned" not in text.splitlines()
    assert f"`build {forged} # pwned`" in text
    assert mrr._code("a\n\tb   c\r\nd") == "a b c d"
    assert mrr._code("a`b``c") == "a'b''c"
    assert mrr.recorded(mrr.Api(REPO, runner=gh), _issue(body=text)) == {
        "ci.yml": ("red", 5010, 1)}


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
    assert gh.calls == [("GET", "issues")] * 2


def test_a_list_that_never_ends_is_refused_not_cut_short(monkeypatch) -> None:
    monkeypatch.setattr(mrr, "MAX_ISSUE_PAGES", 2)
    monkeypatch.setattr(mrr, "MAX_COMMENT_PAGES", 2)
    others = [_issue(1000 + i, f"other {i}") for i in range(mrr.PAGE * 2)]
    with pytest.raises(mrr.ApiError, match="not read to its end"):
        mrr.find_issue(mrr.Api(REPO, runner=FakeGh(issues=[*others, _issue(77)])))
    many = [{"user": BOT, "body": ""}] * (mrr.PAGE * 2)
    with pytest.raises(mrr.ApiError, match="not read to its end"):
        mrr.recorded(mrr.Api(REPO, runner=FakeGh(comments={77: many})), _issue())


def test_markers_are_read_from_every_page_of_comments() -> None:
    filler = [{"user": BOT, "body": "noise"}] * (mrr.PAGE + 5)
    gh = _gh([R(10, "failure")], issues=[_issue()],
             comments={77: [*filler, {"user": BOT, "body": _mark("red", "ci.yml", 10)}]})
    assert _plan(gh).kind == "noop"


# ── GitHub does not answer ──────────────────────────────────────────────────

@pytest.mark.parametrize("ci", [[R(10, "failure")], [R(10, "success")]], ids=["red", "green"])
def test_a_failed_issue_lookup_is_never_no_issue(ci) -> None:
    """⛔ The cell this module exists for: reading a failed lookup as "none
    open" opens a duplicate issue on every run."""
    gh = _gh(ci, issues=[_issue()], fail=("issues",))
    with pytest.raises(mrr.NoVerdict, match="tracking issue"):
        _plan(gh)
    assert gh.written == []


@pytest.mark.parametrize("file", ["ci.yml", "docs.yaml"])
@pytest.mark.parametrize("with_issue", [True, False], ids=["issue-open", "no-issue"])
def test_a_failed_runs_listing_is_never_green(file, with_issue) -> None:
    gh = _gh([R(10, "success")], issues=[_issue()] if with_issue else [],
             fail=(f"actions/workflows/{file}",))
    with pytest.raises(mrr.NoVerdict, match="where main stands"):
        _plan(gh)
    assert gh.written == []


def test_comments_that_cannot_be_read_are_not_taken_as_nothing_recorded() -> None:
    gh = _gh([R(10, "failure")], issues=[_issue(body=_mark("red", "ci.yml", 10))],
             fail=("issues/77/comments",))
    with pytest.raises(mrr.NoVerdict, match="what it says"):
        _plan(gh)
    assert gh.written == []


@pytest.mark.parametrize("runs", [
    {"message": "Bad credentials"}, [None], [{"conclusion": "success"}],
    [{**R(1, "success"), "run_number": True}],
], ids=["object", "null-entry", "no-run-number", "bool-run-number"])
def test_a_runs_listing_in_another_shape_is_refused(runs) -> None:
    gh = FakeGh(runs={"ci.yml": runs})
    with pytest.raises(mrr.ApiError):
        mrr.push_runs(mrr.Api(REPO, runner=gh), "ci.yml")


@pytest.mark.parametrize("patch", [{"id": None}, {"head_sha": ""}, {"html_url": None}],
                         ids=["no-id", "no-sha", "no-url"])
def test_a_newest_run_that_cannot_be_named_is_refused(patch) -> None:
    """Its id goes into the marker: without it nothing can be recorded."""
    gh = _gh([{**R(10, "failure"), **patch}])
    with pytest.raises(mrr.NoVerdict, match="where main stands"):
        _plan(gh)


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


# ── what only describes the failure ─────────────────────────────────────────

def test_what_only_describes_the_failure_does_not_stop_the_report() -> None:
    """The jobs and the commit list are description. Losing them must not
    lose the issue — and each gap is stated, not papered over."""
    gh = _gh([R(10, "failure"), R(8, "success")], fail=("actions/runs/", "compare/"))
    action = _plan(gh)
    assert action.kind == "open"
    assert "the job list could not be read" in action.text
    assert "The commits in the range could not be read" in action.text
    assert _mark("red", "ci.yml", 10) in action.text.splitlines()


@pytest.mark.parametrize("jobs", [
    [{"name": "a", "conclusion": 7}], [{"name": "a", "conclusion": ["failure"]}],
    [{"name": None, "conclusion": "failure"}], ["a"], [None],
    [{"name": "ok", "conclusion": "failure"}, {"conclusion": "failure"}],
], ids=["int-conclusion", "list-conclusion", "no-name", "string-entry", "null-entry",
        "second-entry-bad"])
def test_a_malformed_job_entry_is_a_stated_gap_not_a_crash(jobs) -> None:
    gh = _gh([R(10, "failure")], jobs=jobs)
    action = _plan(gh)
    assert action.kind == "open" and "the job list could not be read" in action.text


@pytest.mark.parametrize("commits", [
    [{"sha": _sha(10)}], [{"sha": _sha(10), "commit": None}],
    [{"sha": 7, "commit": {"message": "x"}}], [{"sha": _sha(10), "commit": {"message": None}}],
    ["x"],
], ids=["no-commit", "null-commit", "int-sha", "null-message", "string-entry"])
def test_a_malformed_commit_entry_is_a_stated_gap_not_a_crash(commits) -> None:
    gh = _gh([R(10, "failure"), R(8, "success")], commits=commits)
    action = _plan(gh)
    assert action.kind == "open"
    assert "The commits in the range could not be read" in action.text


def test_an_empty_commit_message_is_an_empty_subject() -> None:
    gh = _gh([R(10, "failure"), R(8, "success")],
             commits=[{"sha": _sha(10), "commit": {"message": ""}}])
    assert f"{_sha(10)[:9]} \n```" in _plan(gh).text


def test_a_range_start_that_cannot_be_found_is_said_not_invented() -> None:
    gh = _gh([R(12, "failure"), R(11, "cancelled"), R(10, "failure")])
    action = _plan(gh)
    assert f"Range: start unknown .. `{_sha(12)[:9]}`" in action.text
    assert "none of the newest 3 push runs" in action.text
    assert "Commits in the range" not in action.text
    assert not [c for c in gh.calls if c[1].startswith("compare/")]


def test_the_range_starts_at_an_earlier_success_never_a_later_one() -> None:
    """Red is the newest verdict, so a later success cannot exist in the
    state — but the start is still searched below the red run only."""
    standing = mrr.Standing(WATCHED[0], "red", mrr.run_from(R(10, "failure")),
                            (R(11, "success"), R(10, "failure"), R(9, "cancelled"),
                             R(8, "success"), R(7, "success")))
    assert mrr.range_start(standing.run, standing.runs)["run_number"] == 8


def test_a_failure_on_the_commit_that_last_passed_is_called_flaky() -> None:
    gh = _gh([R(10, "failure", sha="f" * 40), R(9, "success", sha="f" * 40)])
    action = _plan(gh)
    assert "nothing was merged in between" in action.text
    assert not [c for c in gh.calls if c[1].startswith("compare/")]


def _range(count: int, head: int = 10) -> list[dict]:
    """`count` commits, oldest first, the last one being run `head`'s sha."""
    return [{"sha": f"{i:040x}" if i < count - 1 else _sha(head),
             "commit": {"message": f"c{i}"}} for i in range(count)]


@pytest.mark.parametrize("count, pages, first_shown", [
    (3, [1], 0),
    (100, [1], 70),
    (101, [1, 2, 1], 71),          # last page holds one: the one before is read too
    (230, [1, 3], 200),
    (329, [1, 4, 3], 299),         # last page holds 29: the one before is read too
    (330, [1, 4], 300),
], ids=["short", "one-full-page", "one-over", "three-pages", "last-page-short", "last-page-30"])
def test_the_newest_commits_of_a_long_range_are_the_ones_shown(count, pages, first_shown) -> None:
    """The compare API answers oldest-first in pages: the first page of a
    long range holds the OLDEST commits, which are the least likely culprits."""
    gh = _gh([R(10, "failure"), R(8, "success")], commits=_range(count))
    text = _plan(gh).text
    shown = text.split("```")[1].strip().splitlines()
    assert shown[-1].startswith(_sha(10)[:9]), shown[-1]
    assert shown[0].endswith(f" c{first_shown}") and len(shown) == min(count, mrr.MAX_COMMITS_SHOWN)
    if count > mrr.MAX_COMMITS_SHOWN:
        assert f"({count}; the newest {mrr.MAX_COMMITS_SHOWN} shown, oldest first)" in text
    else:
        assert f"({count}, oldest first)" in text
    asked = [int(p["page"]) for (m, path), p in zip(gh.calls, gh.params)
             if path.startswith("compare/")]
    assert asked == pages


def test_fewer_commits_than_the_total_and_no_head_is_unreadable_not_shown() -> None:
    """The answer claims 400 commits and hands over 100 that do not end at the
    failing sha: the list is not the newest part of the range, so it is not
    presented as one."""
    gh = _gh([R(10, "failure"), R(8, "success")],
             commits=[{"sha": f"{i:040x}", "commit": {"message": f"c{i}"}} for i in range(100)],
             total=400)
    text = _plan(gh).text
    assert "The commits in the range could not be read" in text and "```" not in text

    class SamePageWhateverIsAsked(FakeGh):
        """A server (or proxy) that ignores `page`: every page is the oldest
        100. Their last sha shares its first 9 characters with the head's."""
        def _compare(self, page, per):
            return super()._compare(1, per)
    gh = SamePageWhateverIsAsked(
        runs={"ci.yml": [R(10, "failure"), R(8, "success")], "docs.yaml": DOCS_GREEN},
        commits=[{"sha": f"{i:040x}", "commit": {"message": f"c{i}"}} for i in range(100)],
        total=150)
    text = _plan(gh).text
    assert "The commits in the range could not be read" in text and "```" not in text
    assert "the newest commit read is not" in text
    assert _sha(10)[:9] == f"{99:040x}"[:9], "the prefix collision this guards against"


def test_a_failed_run_whose_jobs_all_passed_is_said() -> None:
    gh = _gh([R(10, "startup_failure")], jobs=[{"name": "a", "conclusion": "success"}])
    assert "no job of this attempt reported a failing conclusion" in _plan(gh).text


# ── inputs ──────────────────────────────────────────────────────────────────

_PAYLOAD = R(10, "failure")


def test_the_event_payload_is_read() -> None:
    assert mrr.run_from(_PAYLOAD) == _event("failure", workflow="CI", number=10)


@pytest.mark.parametrize("patch", [
    {"conclusion": None}, {"conclusion": ""}, {"id": None}, {"id": "5010"}, {"id": True},
    {"head_sha": ""}, {"name": None}, {"event": None}, {"head_branch": None},
    {"run_attempt": None},
], ids=lambda p: "-".join(f"{k}={v!r}" for k, v in p.items()))
def test_a_payload_with_a_missing_field_is_refused_not_defaulted(patch) -> None:
    with pytest.raises(mrr.NoVerdict):
        mrr.run_from({**_PAYLOAD, **patch})


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
    detector, wf_dir = _tree(tmp_path, ["A", "B", "C", "D", "E", "F", "G"], {
        "a.yml": "name: A\non:\n  push:\n    branches: [main]\n  pull_request:\n",
        "b.yaml": "name: B\non:\n  pull_request:\n",
        "c.yml": "name: C\non: [push, pull_request]\n",
        "d.yml": "name: D\non:\n  push:\n    branches: [release, dev/x]\n",
        "e.yml": "name: E\non: push\n",
        "f.yml": "name: F\non:\n  push:\n  pull_request:\n",
        "g.yml": "name: G\non: pull_request\n"})
    assert mrr.watched_workflows(detector, wf_dir) == [
        mrr.Watched("A", "a.yml", True), mrr.Watched("B", "b.yaml", False),
        mrr.Watched("C", "c.yml", True), mrr.Watched("D", "d.yml", False),
        mrr.Watched("E", "e.yml", True), mrr.Watched("F", "f.yml", True),
        mrr.Watched("G", "g.yml", False)]


@pytest.mark.parametrize("on", [
    "on:\n  push: [main]\n",
    "on:\n  push:\n    branches: main\n",
    "on:\n  push:\n    branches-ignore: [main]\n",
    "on:\n  push:\n    tags: ['v*']\n",
    "on:\n  push:\n    branches: ['ma*']\n",
    "on:\n  push:\n    branches: ['**']\n",
    "on:\n  push:\n    branches: [main]\n    paths: ['docs/**']\n",
    "on:\n  push:\n    branches: [main]\n    tags: ['v*']\n",
    "on:\n  push:\n    branches: []\n",
    "on:\n  push:\n    branches: [main, 3]\n",
    "on:\n  push: main\n",
    "on: [push, {pull_request: null}]\n",
    "jobs: {}\n",
], ids=["push-is-a-list", "branches-string", "branches-ignore", "tags-only", "glob",
        "double-star", "paths", "branches-and-tags", "empty-branches", "non-string-branch",
        "push-is-a-string", "mixed-list", "no-on"])
def test_a_push_trigger_in_an_unmodelled_shape_is_refused_and_names_the_file(tmp_path, on) -> None:
    """Not guessed to run (a workflow that never reports would be waited on)
    and not guessed not to (it would drop out of the recovery test)."""
    detector, wf_dir = _tree(tmp_path, ["A"], {"a.yml": "name: A\n" + on})
    with pytest.raises(mrr.NoVerdict, match=r"a\.yml"):
        mrr.watched_workflows(detector, wf_dir)


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

def _event_file(tmp_path: Path, **patch) -> str:
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"workflow_run": {**_PAYLOAD, **patch}}), encoding="utf-8")
    return str(path)


def _repo_runs(conclusion: str) -> dict:
    return {wf.file: [R(3, conclusion, name=wf.name)] for wf in mrr.watched_workflows()}


def _main(tmp_path, gh, *extra, **patch) -> int:
    return mrr.main(["--repo", REPO, "--event-file", _event_file(tmp_path, **patch), *extra],
                    runner=gh)


WRITE = ("--write",)


def test_exit_codes(tmp_path, capsys) -> None:
    """0 main is not known red and no issue is left open · 1 main is red, or
    the issue is open and main is not known all green — written to or not ·
    2 no verdict."""
    green, red = _repo_runs("success"), _repo_runs("failure")
    known = "\n".join(f"<!-- main-red: red {file} 5003 1 -->" for file in red)
    never = {**green, "validate.yaml": []}
    cases = [
        ("open",              FakeGh(runs=red), {}, 1, 1),
        ("open-on-a-green-event", FakeGh(runs=red), {"conclusion": "success"}, 1, 1),
        ("comment",           FakeGh(issues=[_issue()], runs=red), {}, 1, 1),
        ("nothing-new",       FakeGh(issues=[_issue(body=known)], runs=red), {}, 1, 0),
        ("unknown-issue-open", FakeGh(issues=[_issue()], runs=never), {}, 1, 0),
        ("close",             FakeGh(issues=[_issue()], runs=green), {}, 0, 2),
        ("green-no-issue",    FakeGh(runs=green), {}, 0, 0),
        ("unknown-no-issue",  FakeGh(runs=never), {}, 0, 0),
        ("cancelled",         FakeGh(runs=red), {"conclusion": "cancelled"}, 0, 0),
        ("pull-request",      FakeGh(runs=red), {"event": "pull_request"}, 0, 0),
        ("lookup-failed",     FakeGh(runs=red, fail=("issues",)), {}, 2, 0),
        ("listing-failed",    FakeGh(issues=[_issue()], runs=green,
                                     fail=("actions/workflows/",)), {}, 2, 0),
        ("comments-failed",   FakeGh(issues=[_issue()], runs=red,
                                     fail=("issues/77/comments",)), {}, 2, 0),
        ("no-conclusion",     FakeGh(runs=red), {"conclusion": None}, 2, 0),
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
    assert _main(tmp_path, gh, *WRITE) == 2
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
    required checks never ran on the merged tree."""
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


def test_both_paths_write_as_the_identity_the_script_accepts() -> None:
    """The tracking issue must have been opened by `github-actions[bot]`. The
    event path and the manual replay are one step with one token —
    `github.token` — so an issue the replay opens is one the event path finds.
    A PAT or an App token here would open issues the script then ignores."""
    steps = _detector()["jobs"]["report"]["steps"]
    tokens = [(s.get("name"), k, v) for s in steps for k, v in (s.get("env") or {}).items()
              if "TOKEN" in k.upper()]
    assert tokens == [(steps[-1]["name"], "GH_TOKEN", "${{ github.token }}")], tokens
    assert "secrets." not in DETECTOR.read_text(encoding="utf-8")
    assert (mrr.TRACKER_LOGIN, mrr.TRACKER_TYPE) == ("github-actions[bot]", "Bot")


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
