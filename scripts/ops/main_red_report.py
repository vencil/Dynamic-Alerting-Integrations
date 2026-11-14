"""Open, update or close the one tracking issue for a red `main` (#3922).

Called by `.github/workflows/main-red-detector.yml` each time a workflow that
reports one of main's required checks finishes a `push` run on `main`. Nothing
else tells anyone that main is red: branch protection does not require a PR to
be up to date with main, so two PRs that are each green can break main
together, and the only trace used to be a red run nobody was looking at.

What it does with one finished run (`workflow_run` payload, or `--run-id`):

  not a push run on main, or concluded `cancelled` / `skipped`  -> nothing
  any other conclusion than `success` (failure, timed_out, …):
      no open tracking issue  -> open it
      one is open             -> comment on it
  `success`:
      no open tracking issue  -> nothing
      one is open, and the newest push run WITH A VERDICT of every watched
      workflow is `success`   -> comment and close it
      one is open, otherwise  -> comment (what is still red), leave it open

The report names a RANGE, never "the first bad commit": `<sha of the newest
earlier push run of the same workflow on main that concluded success>..<this
sha>`. A run replaced by the next merge is `cancelled`, so the breaking commit
can be anywhere in that range. When the start cannot be found the report says
so instead of guessing one.

Which workflows are watched is written once: `on.workflow_run.workflows` of
the detector workflow. This script reads it from there, and
tests/ops/test_main_red_report.py holds it equal to the set of workflows that
report an entry of `.github/required-checks.yaml`. A watched workflow without
a `push` trigger for main (commitlint lints a PR's title) never has a run to
judge and is left out of the "everything is green again" test.

The tracking issue is found by EXACT title among the open issues, read through
the REST list (`GET /issues`), not the search API: search matches loosely and
its index lags a just-created issue by seconds, which is the window in which
two workflows failing on one merge would each open one.

Exit codes (scripts/tools/_lib_exitcodes.py contract):
  0  nothing to report: no action was due, or the issue was closed
  1  main is red (or not known to be green): the issue was opened or commented
     on and stays open. Same meaning as required_checks_drift.py's 1 — a
     finding that a change to the repo resolves. The workflow does not fail
     on it; the issue is the signal.
  2  no verdict: the event, the workflow list, or an answer from GitHub that
     the DECISION rests on could not be read, or a write failed.
     ⛔ A failed issue lookup is never "no issue is open": that would open a
     duplicate on every failing run. A failed runs listing is never "green":
     that would close the issue while main is red.
     Answers that only DESCRIBE the failure (the failed jobs, the commits in
     the range, the start of the range) do not stop the report: the issue is
     still filed and says which part could not be read.

Usage:
  python3 scripts/ops/main_red_report.py --repo OWNER/REPO --event-file "$GITHUB_EVENT_PATH" --write
  python3 scripts/ops/main_red_report.py --repo OWNER/REPO --run-id 123

Read-only unless `--write` is given: without it everything is read and the
write that would be made is printed. The exit code is the same either way.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

import yaml  # noqa: E402

EXIT_RED = 1
EXIT_NO_VERDICT = 2

ROOT = Path(os.path.realpath(__file__)).parents[2]
DETECTOR_WORKFLOW = ROOT / ".github/workflows/main-red-detector.yml"
WORKFLOW_DIR = ROOT / ".github/workflows"

# ⛔ The dedup key. Matched exactly; changing it orphans an open issue.
ISSUE_TITLE = "[ci] main is red: a workflow that reports required checks failed on main"

BRANCH = "main"
# Conclusions that say nothing about the tree: the run did not finish its work.
NO_VERDICT = frozenset({"cancelled", "skipped"})
PAGE = 100
# Open issues are paged until a short page; past this many pages the lookup is
# refused (rc 2) rather than cut short and read as "no issue".
MAX_ISSUE_PAGES = 50
MAX_JOB_PAGES = 10
MAX_COMMITS_SHOWN = 30
MAX_JOBS_SHOWN = 50


class NoVerdict(Exception):
    """Something the decision rests on could not be read (exit 2)."""


class ApiError(Exception):
    """One GitHub call failed or answered in a shape this script cannot read."""


# ── GitHub ──────────────────────────────────────────────────────────────────

def _gh(args: list[str], stdin: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120, check=False)


class Api:
    """`gh api`, with the runner injectable so the offline tests drive the
    failure paths through the same code (same seam as file_race_report.Gh)."""

    def __init__(self, repo: str, *, dry_run: bool = False, runner=None) -> None:
        self.repo = repo
        self.dry_run = dry_run
        self._runner = runner or _gh
        self.writes: list[tuple[str, str, dict]] = []

    def get(self, path: str, **params):
        args = ["api", "-X", "GET", f"repos/{self.repo}/{path}"]
        for key, value in params.items():
            args += ["-f", f"{key}={value}"]
        return self._call(args, None, f"GET {path}")

    def write(self, method: str, path: str, body: dict):
        self.writes.append((method, path, body))
        if self.dry_run:
            rest = {k: v for k, v in body.items() if k != "body"}
            print(f"DRY RUN — would {method} {path} {json.dumps(rest, ensure_ascii=False)}")
            if "body" in body:
                print("┌── body")
                print(body["body"])
                print("└──")
            return {}
        return self._call(["api", "-X", method, f"repos/{self.repo}/{path}", "--input", "-"],
                          json.dumps(body), f"{method} {path}")

    def _call(self, args: list[str], stdin: str | None, what: str):
        try:
            proc = self._runner(args, stdin)
        except (OSError, subprocess.SubprocessError) as err:
            raise ApiError(f"{what}: gh could not run — {err}") from err
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise ApiError(f"{what}: gh exited {proc.returncode}"
                           + (f" — {detail[-1][:200]}" if detail else ""))
        try:
            return json.loads(proc.stdout)
        except ValueError as err:
            raise ApiError(f"{what}: the answer is not JSON — {err}") from err


# ── inputs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Run:
    workflow: str
    run_id: int
    attempt: int
    number: int
    event: str
    branch: str
    sha: str
    conclusion: str
    url: str


def run_from(payload) -> Run:
    """A run object as GitHub sends it (`workflow_run` in the event payload, or
    `GET /actions/runs/{id}`). Every field is required: a missing one is a
    payload this script does not understand, never a default."""
    if not isinstance(payload, dict):
        raise NoVerdict("the run payload is not an object")
    wanted = (("name", str), ("id", int), ("run_attempt", int), ("run_number", int),
              ("event", str), ("head_branch", str), ("head_sha", str), ("html_url", str))
    values = []
    for key, kind in wanted:
        value = payload.get(key)
        if not isinstance(value, kind) or isinstance(value, bool) or value == "":
            raise NoVerdict(f"the run payload has no usable `{key}` ({value!r})")
        values.append(value)
    conclusion = payload.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion:
        raise NoVerdict(f"the run has no conclusion yet ({conclusion!r}) — it is "
                        "not finished, so there is nothing to report")
    name, run_id, attempt, number, event, branch, sha, url = values
    return Run(name, run_id, attempt, number, event, branch, sha, conclusion, url)


@dataclass(frozen=True)
class Watched:
    name: str
    file: str
    runs_on_main_push: bool


def _on(doc: dict):
    """`on:` — YAML 1.1 reads the bare key as the boolean True."""
    return doc.get("on", doc.get(True))


def _has_main_push(doc: dict) -> bool:
    on = _on(doc)
    if isinstance(on, str):
        return on == "push"
    if isinstance(on, list):
        return "push" in on
    if not isinstance(on, dict) or "push" not in on:
        return False
    push = on["push"] or {}
    branches = push.get("branches")
    # `branches-ignore` / tag-only filters are not modelled: none of the
    # watched workflows uses them, and the structure test would have to learn
    # them first.
    return branches is None or BRANCH in branches


def watched_workflows(detector: Path = DETECTOR_WORKFLOW,
                      workflow_dir: Path = WORKFLOW_DIR) -> list[Watched]:
    """The detector's `on.workflow_run.workflows`, resolved to workflow files."""
    try:
        doc = yaml.safe_load(detector.read_text(encoding="utf-8"))
        names = _on(doc)["workflow_run"]["workflows"]
        by_name: dict[str, list[tuple[str, dict]]] = {}
        for path in sorted(workflow_dir.glob("*.y*ml")):
            other = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(other, dict) and isinstance(other.get("name"), str):
                by_name.setdefault(other["name"], []).append((path.name, other))
    except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError) as err:
        raise NoVerdict(f"cannot read the watched workflows from {detector.name} — {err!r}") from err
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise NoVerdict(f"{detector.name}: `on.workflow_run.workflows` is not a list of names")
    watched = []
    for name in names:
        found = by_name.get(name, [])
        if len(found) != 1:
            raise NoVerdict(f"{detector.name} watches {name!r}, and {len(found)} workflow "
                            f"files carry that `name:` ({[f for f, _ in found]}) — exactly "
                            "one must")
        watched.append(Watched(name, found[0][0], _has_main_push(found[0][1])))
    return watched


# ── reads the decision rests on ─────────────────────────────────────────────

def find_issue(api: Api) -> dict | None:
    """The open tracking issue (lowest number if several), or None.

    ⛔ Raises ApiError when the list cannot be read to its end; the caller
    must not treat that as None."""
    matches = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        items = api.get("issues", state="open", per_page=PAGE, page=page)
        if not isinstance(items, list):
            raise ApiError(f"GET issues: expected a list, got {type(items).__name__}")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("number"), int):
                raise ApiError(f"GET issues: unrecognised entry {str(item)[:80]!r}")
            # The issues list includes pull requests.
            if "pull_request" not in item and item.get("title") == ISSUE_TITLE:
                matches.append(item)
        if len(items) < PAGE:
            return min(matches, key=lambda i: i["number"]) if matches else None
    raise ApiError(f"GET issues: more than {MAX_ISSUE_PAGES * PAGE} open issues and "
                   "pull requests — the list was not read to its end")


def push_runs(api: Api, workflow_file: str) -> list[dict]:
    """The newest push runs of one workflow on main, newest first."""
    answer = api.get(f"actions/workflows/{workflow_file}/runs",
                     branch=BRANCH, event="push", per_page=PAGE)
    runs = answer.get("workflow_runs") if isinstance(answer, dict) else None
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        raise ApiError(f"GET actions/workflows/{workflow_file}/runs: no `workflow_runs` list")
    return runs


def latest_verdict(runs: list[dict]) -> dict | None:
    """The newest run that finished with a verdict. A run being re-run is
    `in_progress` with no conclusion and is passed over, like a cancelled one."""
    for run in runs:
        if run.get("status") == "completed" and run.get("conclusion") \
                and run["conclusion"] not in NO_VERDICT:
            return run
    return None


@dataclass(frozen=True)
class State:
    green: tuple[str, ...]
    red: tuple[tuple[str, dict], ...]        # (workflow name, its newest run with a verdict)
    unknown: tuple[str, ...]                 # no run with a verdict among the newest PAGE
    never: tuple[str, ...]                   # no push trigger for main


def main_state(api: Api, watched: list[Watched]) -> State:
    """Where every watched workflow stands on main. ⛔ ApiError propagates: one
    unreadable listing must not be read as green."""
    green, red, unknown, never = [], [], [], []
    for wf in watched:
        if not wf.runs_on_main_push:
            never.append(wf.name)
            continue
        verdict = latest_verdict(push_runs(api, wf.file))
        if verdict is None:
            unknown.append(wf.name)
        elif verdict["conclusion"] == "success":
            green.append(wf.name)
        else:
            red.append((wf.name, verdict))
    return State(tuple(green), tuple(red), tuple(unknown), tuple(never))


# ── reads that only describe the failure ────────────────────────────────────

def _code(text) -> str:
    """One line of untrusted text for a code span or block: a commit subject
    or a job name must not be able to ping a user, link an issue or close the
    fence it sits in."""
    return " ".join(str(text).split()).replace("`", "'")[:120]


def range_start(api: Api, run: Run, workflow_file: str) -> tuple[dict | None, str]:
    """The newest earlier push run of the same workflow on main that concluded
    success, and — when there is none to name — why."""
    try:
        runs = push_runs(api, workflow_file)
    except ApiError as err:
        return None, f"the runs listing could not be read ({err})"
    for other in runs:
        if isinstance(other.get("run_number"), int) and other["run_number"] < run.number \
                and other.get("status") == "completed" and other.get("conclusion") == "success" \
                and isinstance(other.get("head_sha"), str):
            return other, ""
    return None, (f"none of the newest {len(runs)} push runs of this workflow on "
                  f"{BRANCH} before this one concluded `success` (older ones were not read)")


def failed_jobs(api: Api, run: Run) -> list[str]:
    """Lines for the jobs of THIS attempt that failed. The per-attempt
    endpoint, not `runs/{id}/jobs`: that one answers for the latest attempt,
    which a re-run started meanwhile would have replaced."""
    lines, total = [], 0
    try:
        for page in range(1, MAX_JOB_PAGES + 1):
            answer = api.get(f"actions/runs/{run.run_id}/attempts/{run.attempt}/jobs",
                             per_page=PAGE, page=page)
            jobs = answer.get("jobs") if isinstance(answer, dict) else None
            if not isinstance(jobs, list):
                raise ApiError("no `jobs` list in the answer")
            for job in jobs:
                conclusion = job.get("conclusion") if isinstance(job, dict) else None
                if conclusion in (None, "success", "neutral") or conclusion in NO_VERDICT:
                    continue
                total += 1
                if total <= MAX_JOBS_SHOWN:
                    lines.append(f"  - `{_code(job.get('name'))}` ({_code(conclusion)})")
            if len(jobs) < PAGE:
                break
        else:
            lines.append(f"  - ⚠️ only the first {MAX_JOB_PAGES * PAGE} jobs were read")
    except ApiError as err:
        return lines + [f"  - ⚠️ the job list could not be read ({_code(err)}) — open the run"]
    if not total:
        return [f"  - no job of this attempt reported a failing conclusion; the run "
                f"itself concluded `{run.conclusion}` — open the run"]
    if total > MAX_JOBS_SHOWN:
        lines.append(f"  - … and {total - MAX_JOBS_SHOWN} more")
    return lines


def commits_in_range(api: Api, base: str, head: str) -> list[str]:
    if base == head:
        return ["Same commit as the last successful run: nothing was merged in between, "
                "so this is a flaky or environment-dependent failure, not a new commit."]
    try:
        answer = api.get(f"compare/{base}...{head}", per_page=PAGE)
        commits = answer.get("commits") if isinstance(answer, dict) else None
        if not isinstance(commits, list):
            raise ApiError("no `commits` list in the answer")
        rows = [f"{c['sha'][:9]} {_code(c['commit']['message'].splitlines()[0] if c['commit']['message'] else '')}"
                for c in commits]
        total = answer.get("total_commits")
    except (ApiError, KeyError, TypeError, IndexError) as err:
        return [f"⚠️ The commits in the range could not be read ({_code(err)!s})."]
    if not isinstance(total, int):
        total = len(rows)
    shown = rows[-MAX_COMMITS_SHOWN:]
    head_line = f"Commits in the range ({total}"
    if len(shown) < total:
        head_line += f"; the newest {len(shown)} shown"
    return [head_line + ", oldest first):", "", "```", *shown, "```"]


def failure_report(api: Api, run: Run, workflow_file: str) -> str:
    start, why_not = range_start(api, run, workflow_file)
    lines = [f"**{_code(run.workflow)}** concluded `{_code(run.conclusion)}` on `{BRANCH}` "
             f"(run attempt {run.attempt}): {run.url}", ""]
    if start:
        lines.append(f"- Range: `{start['head_sha'][:9]}..{run.sha[:9]}` — from the newest "
                     f"earlier push run of this workflow that concluded `success` "
                     f"({start.get('html_url', 'run ' + str(start.get('id')))}) to this run. "
                     "The breaking commit is somewhere in it; runs replaced by a later "
                     "merge are `cancelled`, so no single commit can be named.")
    else:
        lines.append(f"- Range: start unknown .. `{run.sha[:9]}` — {why_not}.")
    lines.append(f"- Failed jobs (run attempt {run.attempt}):")
    lines += failed_jobs(api, run)
    if start:
        lines += ["", *commits_in_range(api, start["head_sha"], run.sha)]
    return "\n".join(lines)


# ── the decision ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Plan:
    kind: str          # noop | open | comment | close
    why: str
    text: str = ""
    issue: int | None = None


_OPEN_HEADER = (
    "A workflow that reports one of `main`'s required checks failed on a push to "
    "`main`. Until it is green again, every PR that syncs with `main` fails the "
    "same check.\n\n"
    "This issue is maintained by `.github/workflows/main-red-detector.yml`: each "
    "further failing run adds a comment, and the issue is closed automatically "
    "once the newest push run with a verdict of every watched workflow is "
    "`success` (a successful re-run counts).\n\n---\n\n")


def plan(run: Run, watched: list[Watched], api: Api) -> Plan:
    """What to do about one finished run. Reads GitHub; writes nothing."""
    if run.event != "push" or run.branch != BRANCH:
        return Plan("noop", f"not a push run on {BRANCH} (event {run.event!r}, "
                            f"branch {run.branch!r})")
    if run.conclusion in NO_VERDICT:
        return Plan("noop", f"the run concluded `{run.conclusion}` — neither a failure "
                            "nor a recovery")
    by_name = {wf.name: wf for wf in watched}
    if run.workflow not in by_name:
        raise NoVerdict(f"{run.workflow!r} is not one of the watched workflows "
                        f"({sorted(by_name)}) — the trigger list and this event disagree")
    try:
        issue = find_issue(api)
    except ApiError as err:
        # ⛔ Not "no issue": that opens a duplicate on every failing run.
        raise NoVerdict(f"cannot tell whether the tracking issue is open — {err}") from err
    number = issue["number"] if issue else None

    if run.conclusion != "success":
        report = failure_report(api, run, by_name[run.workflow].file)
        if issue is None:
            return Plan("open", f"{run.workflow} failed and no tracking issue is open",
                        _OPEN_HEADER + report)
        return Plan("comment", f"{run.workflow} failed; tracking issue #{number} is open",
                    report, number)

    if issue is None:
        return Plan("noop", f"{run.workflow} succeeded and no tracking issue is open")
    try:
        state = main_state(api, watched)
    except ApiError as err:
        # ⛔ Not "green": that closes the issue while main is red.
        raise NoVerdict(f"cannot tell whether {BRANCH} is green again — {err}") from err
    ran = f"**{_code(run.workflow)}** succeeded at `{run.sha[:9]}` (run attempt {run.attempt}): {run.url}"
    if not state.red and not state.unknown:
        text = (f"✅ `{BRANCH}` is green again as of `{run.sha[:9]}`. {ran}\n\n"
                "The newest push run with a verdict of every watched workflow is `success`: "
                + ", ".join(f"`{_code(n)}`" for n in state.green) + ".")
        if state.never:
            text += ("\n\nNot judged (no `push` trigger for `main`): "
                     + ", ".join(f"`{_code(n)}`" for n in state.never) + ".")
        return Plan("close", f"every watched workflow is green on {BRANCH}", text, number)
    lines = [ran, "", f"Not closing: `{BRANCH}` is not green in every watched workflow."]
    for name, verdict in state.red:
        lines.append(f"- still red: `{_code(name)}` — `{_code(verdict.get('conclusion'))}` at "
                     f"`{str(verdict.get('head_sha'))[:9]}` ({verdict.get('html_url')})")
    for name in state.unknown:
        lines.append(f"- unknown: `{_code(name)}` — none of its newest {PAGE} push runs on "
                     f"`{BRANCH}` finished with a verdict")
    return Plan("comment", f"{run.workflow} succeeded but {BRANCH} is not all green",
                "\n".join(lines), number)


def apply(action: Plan, api: Api) -> None:
    """Carry out a plan. ApiError propagates: a write that failed is exit 2."""
    if action.kind == "open":
        api.write("POST", "issues", {"title": ISSUE_TITLE, "body": action.text})
    elif action.kind in ("comment", "close"):
        api.write("POST", f"issues/{action.issue}/comments", {"body": action.text})
        if action.kind == "close":
            api.write("PATCH", f"issues/{action.issue}",
                      {"state": "closed", "state_reason": "completed"})


# ── entry ───────────────────────────────────────────────────────────────────

def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main_red_report.py",
        description="Open, update or close the tracking issue for a red main (#3922).")
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--event-file", help="a workflow_run event payload ($GITHUB_EVENT_PATH)")
    source.add_argument("--run-id", type=int, help="judge this finished run instead of an event")
    parser.add_argument("--write", action="store_true",
                        help="open / comment on / close the issue; without it the "
                             "write is printed, not made")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, runner=None) -> int:
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    api = Api(args.repo, dry_run=not args.write, runner=runner)
    try:
        if args.event_file:
            with open(args.event_file, encoding="utf-8") as fh:
                event = json.load(fh)
            payload = event.get("workflow_run") if isinstance(event, dict) else None
            if payload is None:
                raise NoVerdict("the event has no `workflow_run` object")
        else:
            payload = api.get(f"actions/runs/{args.run_id}")
        run = run_from(payload)
        action = plan(run, watched_workflows(), api)
        apply(action, api)
    except (NoVerdict, ApiError, OSError, ValueError) as err:
        sys.stderr.write(f"main_red_report: no verdict — {err}\n")
        return EXIT_NO_VERDICT
    target = f" #{action.issue}" if action.issue else ""
    print(f"main_red_report: {action.kind}{target} — {action.why}")
    return EXIT_RED if action.kind in ("open", "comment") else 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
