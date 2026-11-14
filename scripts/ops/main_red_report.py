"""Open, update or close the one tracking issue for a red `main` (#3922).

Called by `.github/workflows/main-red-detector.yml` each time a workflow that
reports one of main's required checks finishes a `push` run on `main`. Nothing
else tells anyone that main is red: branch protection does not require a PR to
be up to date with main, so two PRs that are each green can break main
together, and the only trace used to be a red run nobody was looking at.

The finished run only says WHEN to look. What is done follows from main's
STATE, read afresh every time, so an event that was never delivered (the
workflow keeps one pending run; a third event replaces it) is made up for by
the next one:

  the event is not a push run on main, or concluded `cancelled` / `skipped`
                                   -> nothing, and nothing is read (exit 0)

  otherwise, for every watched workflow, take its NEWEST push run on main
  that has a verdict — the highest `run_number` whose latest attempt is
  completed with a conclusion other than `cancelled` / `skipped`. It is
  red (any conclusion but `success`), green, or unknown (no run with a
  verdict among the newest 100 — e.g. the workflow has not finished a push
  run yet; that is not "unreadable" — or a re-run that is still going or was
  cancelled sits above every verdict: its earlier attempt's verdict is not
  in the listing, and the run below it is not main's state). Then:

  some workflow red     no tracking issue -> open one, reporting every red run
                        issue open        -> comment only with what the issue
                                             does not say yet: a red run it
                                             has not recorded, or a workflow
                                             it recorded as red that is green
                                             now. Nothing new -> no write.
  none red, none unknown   issue open     -> comment and close it
                           no issue       -> nothing
  none red, some unknown   issue open     -> not closed; a recovery is
                                             commented as above
                           no issue       -> nothing (exit 0: nothing is
                                             known to be red)

"Newest" is by `run_number` — the order the pushes arrived in, which on main
is commit order — never by when a run finished: runs finish out of order, and
a re-run of an old run finishes long after its successors. The same run
re-run is one entry carrying its latest attempt, so a run re-run to success is
green and an old run re-run into another failure changes nothing.

What the issue "has recorded" is read back from marker lines this script
writes into the issue body and its own comments, one per reported run:
`<!-- main-red: red|green <workflow file> <run id> <attempt> -->`. The last
marker of a workflow wins. Markers are read only from text written by the
tracking issue's author, and only as whole lines; the report puts every piece
of text it did not generate (commit subjects, job names) inside code spans.

The tracking issue is the OPEN issue with exactly `ISSUE_TITLE` that
`github-actions[bot]` (type `Bot`) opened — the identity of `github.token`,
which is what both the event path and the manual replay run under. An issue
with the same title opened by anyone else is not it: the repo is public, and
without this an outsider's issue would swallow the report and then be
commented on and closed with this workflow's token. The list is asked for by
creator and each hit is checked again here.

The report names a RANGE, never "the first bad commit": `<sha of the newest
earlier push run of the same workflow that concluded success>..<sha of the
red run>`. A run replaced by the next merge is `cancelled`, so the breaking
commit can be anywhere in that range. When the start cannot be found the
report says so instead of guessing one.

Which workflows are watched is written once: `on.workflow_run.workflows` of
the detector workflow. This script reads it from there, and
tests/ops/test_main_red_report.py holds it equal to the set of workflows that
report an entry of `.github/required-checks.yaml`. A watched workflow without
a `push` trigger for main (commitlint lints a PR's title) never has a run to
judge and is left out. A `push` trigger in a shape this script does not model
(`branches-ignore`, `tags`, `paths`, a glob, a bare string) is exit 2: it is
not guessed to run, nor not to.

Exit codes (scripts/tools/_lib_exitcodes.py contract):
  0  main is not known to be red and no tracking issue is left open: nothing
     was due, or the issue was closed.
  1  this run found main red, or found the tracking issue open while main is
     not known to be all green. The issue was opened, commented on, or
     already said everything. Same meaning as required_checks_drift.py's 1 —
     a finding that a change to the repo resolves. The workflow does not
     fail on it; the issue is the signal.
  2  no verdict: the event, the workflow list, a runs listing, the issue
     list, the issue's comments — anything the DECISION rests on — could not
     be read, or a write failed.
     ⛔ A failed issue lookup is never "no issue is open": that would open a
     duplicate on every run. A failed runs listing is never "green": that
     would close the issue while main is red.
     Answers that only DESCRIBE a failure (its failed jobs, the commits in
     the range) do not stop the report: the issue is still filed and says
     which part could not be read.

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
import re
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
# Who must have opened the tracking issue (and written a comment for its
# markers to count): the identity of `github.token`. Literal values read from
# this repo's bot-opened issues (user.login / user.type).
TRACKER_LOGIN = "github-actions[bot]"
TRACKER_TYPE = "Bot"

BRANCH = "main"
# Conclusions that say nothing about the tree: the run did not finish its work.
NO_VERDICT = frozenset({"cancelled", "skipped"})
PAGE = 100
# Lists are paged until a short page; past this many pages the read is refused
# (rc 2) rather than cut short and read as "nothing there".
MAX_ISSUE_PAGES = 20
MAX_COMMENT_PAGES = 30
MAX_JOB_PAGES = 10
MAX_COMMITS_SHOWN = 30
MAX_JOBS_SHOWN = 50

_WORKFLOW_FILE = re.compile(r"[A-Za-z0-9._-]+\Z")
MARKER = re.compile(r"^<!-- main-red: (red|green) ([A-Za-z0-9._-]+) (\d+) (\d+) -->$", re.M)


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

    def pages(self, path: str, max_pages: int, **params) -> list:
        """Every item of a list endpoint. ⛔ Never a prefix: a non-list page,
        or a list still going at `max_pages`, is an ApiError."""
        items: list = []
        for page in range(1, max_pages + 1):
            chunk = self.get(path, per_page=PAGE, page=page, **params)
            if not isinstance(chunk, list):
                raise ApiError(f"GET {path}: expected a list, got {type(chunk).__name__}")
            items += chunk
            if len(chunk) < PAGE:
                return items
        raise ApiError(f"GET {path}: more than {max_pages * PAGE} entries — the list "
                       "was not read to its end")


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
    """A run object as GitHub sends it (`workflow_run` in the event payload,
    `GET /actions/runs/{id}`, or an entry of a runs listing). Every field is
    required: a missing one is a payload this script does not understand,
    never a default."""
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


def has_main_push(doc, where: str) -> bool:
    """Whether a workflow starts on every push to main.

    ⛔ Only the shapes below are modelled. Anything else — `branches-ignore`,
    `tags`, `paths`, a glob, a string where a list belongs — raises NoVerdict
    naming the file: guessing "runs" hides a workflow that never reports,
    guessing "does not run" drops it from the recovery test."""
    on = _on(doc) if isinstance(doc, dict) else None
    if isinstance(on, str):
        return on == "push"
    if isinstance(on, list) and all(isinstance(event, str) for event in on):
        return "push" in on
    if not isinstance(on, dict):
        raise NoVerdict(f"{where}: `on:` is {type(on).__name__} — not a shape this script models")
    if "push" not in on:
        return False
    push = on["push"]
    if push is None or push == {}:
        return True
    if not isinstance(push, dict) or set(push) != {"branches"}:
        raise NoVerdict(f"{where}: `on.push` is {push!r} — only `branches: [names]` is "
                        "modelled; whether this workflow runs on a push to "
                        f"{BRANCH} is not guessed")
    branches = push["branches"]
    if not isinstance(branches, list) or not branches or not all(
            isinstance(b, str) and re.fullmatch(r"[A-Za-z0-9._/-]+", b) for b in branches):
        raise NoVerdict(f"{where}: `on.push.branches` is {branches!r} — only a list of "
                        "literal branch names is modelled (no glob, no bare string)")
    return BRANCH in branches


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
        file, other = found[0]
        if not _WORKFLOW_FILE.match(file):
            raise NoVerdict(f"workflow file name {file!r} cannot be written into a marker")
        watched.append(Watched(name, file, has_main_push(other, file)))
    return watched


# ── reads the decision rests on ─────────────────────────────────────────────

def _is_tracker(user) -> bool:
    return isinstance(user, dict) and user.get("login") == TRACKER_LOGIN \
        and user.get("type") == TRACKER_TYPE


def find_issue(api: Api) -> dict | None:
    """The open tracking issue (lowest number if several), or None.

    ⛔ Raises ApiError when the list cannot be read to its end; the caller
    must not treat that as None. ⛔ An issue not opened by the tracker
    identity is never returned, whatever its title: the server is asked for
    that creator AND each entry is checked here."""
    matches = []
    for item in api.pages("issues", MAX_ISSUE_PAGES, state="open", creator=TRACKER_LOGIN):
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            raise ApiError(f"GET issues: unrecognised entry {str(item)[:80]!r}")
        # The issues list includes pull requests.
        if "pull_request" not in item and item.get("title") == ISSUE_TITLE \
                and _is_tracker(item.get("user")):
            matches.append(item)
    return min(matches, key=lambda i: i["number"]) if matches else None


def recorded(api: Api, issue: dict) -> dict[str, tuple[str, int, int]]:
    """What the tracking issue already says, per workflow file: the last
    `(red|green, run id, attempt)` marker in its body and in the comments its
    author wrote, oldest first. Anyone can comment on a public issue; a marker
    in someone else's comment is text, not a record."""
    texts = [issue.get("body")]
    for comment in api.pages(f"issues/{issue['number']}/comments", MAX_COMMENT_PAGES):
        if not isinstance(comment, dict):
            raise ApiError(f"GET issues/{issue['number']}/comments: unrecognised entry")
        if _is_tracker(comment.get("user")):
            texts.append(comment.get("body"))
    seen: dict[str, tuple[str, int, int]] = {}
    for text in texts:
        if isinstance(text, str):
            # GitHub stores bodies as sent; a CRLF body must still match `$`.
            for colour, file, run_id, attempt in MARKER.findall(text.replace("\r\n", "\n")):
                seen[file] = (colour, int(run_id), int(attempt))
    return seen


def marker(colour: str, file: str, run: Run) -> str:
    return f"<!-- main-red: {colour} {file} {run.run_id} {run.attempt} -->"


def is_verdict(run: dict) -> bool:
    """A run whose latest attempt finished and says something about the tree.
    ⛔ `status` must be `completed`: a run in progress has `conclusion: null`,
    and one being re-run still shows nothing — neither is green or red."""
    return run.get("status") == "completed" and isinstance(run.get("conclusion"), str) \
        and bool(run["conclusion"]) and run["conclusion"] not in NO_VERDICT


def push_runs(api: Api, workflow_file: str) -> list[dict]:
    """The newest push runs of one workflow on main, highest `run_number`
    first — sorted here, not trusted from the listing's own order."""
    answer = api.get(f"actions/workflows/{workflow_file}/runs",
                     branch=BRANCH, event="push", per_page=PAGE)
    runs = answer.get("workflow_runs") if isinstance(answer, dict) else None
    if not isinstance(runs, list) or not all(
            isinstance(r, dict) and isinstance(r.get("run_number"), int)
            and not isinstance(r.get("run_number"), bool) for r in runs):
        raise ApiError(f"GET actions/workflows/{workflow_file}/runs: no `workflow_runs` "
                       "list of runs with a `run_number`")
    return sorted(runs, key=lambda r: r["run_number"], reverse=True)


@dataclass(frozen=True)
class Standing:
    """Where one watched workflow stands on main."""
    wf: Watched
    colour: str                    # red | green | unknown
    run: Run | None                # its newest run with a verdict
    runs: tuple = ()               # its listing, newest first
    why: str = ""                  # for `unknown`: what stood in the way


def main_state(api: Api, watched: list[Watched]) -> list[Standing]:
    """Every watched workflow that runs on push to main. ⛔ ApiError
    propagates: one unreadable listing must not be read as green."""
    state = []
    for wf in watched:
        if not wf.runs_on_main_push:
            continue
        runs = push_runs(api, wf.file)
        newest, why = None, (f"none of its newest {len(runs)} push runs on `{BRANCH}` "
                             "finished with a verdict")
        for candidate in runs:
            if is_verdict(candidate):
                newest = candidate
                break
            if candidate.get("run_attempt") != 1:
                # A re-run that is still going, or was cancelled: the listing
                # shows only this attempt, and the verdict of the earlier one
                # is gone from it. Falling through to an OLDER run would call a
                # workflow green while its newest run failed and is being
                # retried — and close the issue on it.
                why = (f"run #{candidate['run_number']} is being re-run or its re-run "
                       "was cancelled; its earlier verdict is not in the listing")
                break
        if newest is None:
            state.append(Standing(wf, "unknown", None, tuple(runs), why))
            continue
        try:
            run = run_from(newest)
        except NoVerdict as err:
            raise ApiError(f"{wf.file}: its newest run with a verdict is unreadable — {err}") from err
        state.append(Standing(wf, "green" if run.conclusion == "success" else "red",
                              run, tuple(runs)))
    return state


# ── reads that only describe the failure ────────────────────────────────────

def _code(text) -> str:
    """One line of untrusted text for a code span or block: a commit subject
    or a job name must not be able to ping a user, link an issue, start a new
    line (and with it a marker or a heading) or close the span it sits in."""
    return " ".join(str(text).split()).replace("`", "'")[:120]


def range_start(run: Run, runs: tuple) -> dict | None:
    """The newest earlier push run of the same workflow that concluded success."""
    for other in runs:
        if other["run_number"] < run.number and other.get("status") == "completed" \
                and other.get("conclusion") == "success" \
                and isinstance(other.get("head_sha"), str) and other["head_sha"]:
            return other
    return None


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
                if not isinstance(job, dict) or not isinstance(job.get("name"), str) \
                        or not isinstance(job.get("conclusion"), (str, type(None))):
                    raise ApiError(f"unrecognised job entry {str(job)[:60]!r}")
                conclusion = job["conclusion"]
                if conclusion in (None, "success", "neutral") or conclusion in NO_VERDICT:
                    continue
                total += 1
                if total <= MAX_JOBS_SHOWN:
                    lines.append(f"  - `{_code(job['name'])}` ({_code(conclusion)})")
            if len(jobs) < PAGE:
                break
        else:
            lines.append(f"  - ⚠️ only the first {MAX_JOB_PAGES * PAGE} jobs were read")
    except ApiError as err:
        return lines + [f"  - ⚠️ the job list could not be read (`{_code(err)}`) — open the run"]
    if not total:
        return [f"  - no job of this attempt reported a failing conclusion; the run "
                f"itself concluded `{_code(run.conclusion)}` — open the run"]
    if total > MAX_JOBS_SHOWN:
        lines.append(f"  - … and {total - MAX_JOBS_SHOWN} more")
    return lines


def _compare_page(api: Api, base: str, head: str, page: int) -> tuple[int, list[tuple[str, str]]]:
    """One page of `base...head`: (total_commits, [(full sha, subject)])."""
    answer = api.get(f"compare/{base}...{head}", per_page=PAGE, page=page)
    if not isinstance(answer, dict) or not isinstance(answer.get("commits"), list) \
            or not isinstance(answer.get("total_commits"), int):
        raise ApiError("no `commits` list / `total_commits` in the answer")
    rows = []
    for commit in answer["commits"]:
        try:
            sha, message = commit["sha"], commit["commit"]["message"]
        except (KeyError, TypeError) as err:
            raise ApiError(f"unrecognised commit entry ({err!r})") from err
        if not isinstance(sha, str) or not isinstance(message, str):
            raise ApiError("unrecognised commit entry")
        rows.append((sha, _code((message.splitlines() or [''])[0])))
    return answer["total_commits"], rows


def commits_in_range(api: Api, base: str, head: str) -> list[str]:
    """The newest commits of `base..head`, oldest first. The compare API
    answers oldest-first in pages, so the newest ones are on the LAST page;
    the last row must be `head`, or the pages were not what they were taken
    for and the list is reported as unreadable rather than shown."""
    if base == head:
        return ["Same commit as the last successful run: nothing was merged in between, "
                "so this is a flaky or environment-dependent failure, not a new commit."]
    try:
        total, rows = _compare_page(api, base, head, 1)
        if total > len(rows):
            last = -(-total // PAGE)
            _, rows = _compare_page(api, base, head, last)
            if len(rows) < MAX_COMMITS_SHOWN and last - 1 >= 1:
                rows = _compare_page(api, base, head, last - 1)[1] + rows
        # The FULL sha: a prefix would pass on a page that is not the last.
        if not rows or rows[-1][0] != head:
            raise ApiError(f"the newest commit read is not {head[:9]}")
    except ApiError as err:
        return [f"⚠️ The commits in the range could not be read (`{_code(err)}`)."]
    shown = [f"{sha[:9]} {subject}" for sha, subject in rows[-MAX_COMMITS_SHOWN:]]
    head_line = f"Commits in the range ({total}"
    if len(shown) < total:
        head_line += f"; the newest {len(shown)} shown"
    return [head_line + ", oldest first):", "", "```", *shown, "```"]


def failure_report(api: Api, standing: Standing) -> str:
    """One red workflow: marker, range, failed jobs, commits."""
    run, wf = standing.run, standing.wf
    start = range_start(run, standing.runs)
    lines = [marker("red", wf.file, run),
             f"**{_code(wf.name)}** concluded `{_code(run.conclusion)}` on `{BRANCH}` at "
             f"`{run.sha[:9]}` (run #{run.number}, attempt {run.attempt}): {run.url}", ""]
    if start:
        lines.append(f"- Range: `{start['head_sha'][:9]}..{run.sha[:9]}` — from the newest "
                     f"earlier push run of this workflow that concluded `success` "
                     f"(run #{start['run_number']}) to this run. The breaking commit is "
                     "somewhere in it; runs replaced by a later merge are `cancelled`, so "
                     "no single commit can be named.")
    else:
        lines.append(f"- Range: start unknown .. `{run.sha[:9]}` — none of the newest "
                     f"{len(standing.runs)} push runs of this workflow on `{BRANCH}` before "
                     "this one concluded `success` (older ones were not read).")
    lines.append(f"- Failed jobs (attempt {run.attempt}):")
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
    # main is red, or the issue is open and main is not known all green (exit 1).
    red: bool = False


_OPEN_HEADER = (
    "A workflow that reports one of `main`'s required checks is failing on "
    "`main`. Until it is green again, every PR that syncs with `main` fails the "
    "same check.\n\n"
    "This issue is maintained by `.github/workflows/main-red-detector.yml`: a "
    "further failing run or a workflow turning green again adds a comment, and "
    "the issue is closed automatically once the newest push run with a verdict "
    "of every watched workflow is `success` (a successful re-run counts).\n")


def _still_lines(state: list[Standing], skip: set[str]) -> list[str]:
    lines = []
    for s in state:
        if s.wf.file in skip:
            continue
        if s.colour == "red":
            lines.append(f"- still red: `{_code(s.wf.name)}` — `{_code(s.run.conclusion)}` at "
                         f"`{s.run.sha[:9]}` ({s.run.url})")
        elif s.colour == "unknown":
            lines.append(f"- unknown: `{_code(s.wf.name)}` — {s.why}")
    return lines


def plan(event: Run, watched: list[Watched], api: Api) -> Plan:
    """What main's state calls for, now that `event` finished. Reads GitHub;
    writes nothing."""
    if event.event != "push" or event.branch != BRANCH:
        return Plan("noop", f"not a push run on {BRANCH} (event {event.event!r}, "
                            f"branch {event.branch!r})")
    if event.conclusion in NO_VERDICT:
        return Plan("noop", f"the run concluded `{event.conclusion}` — it says nothing "
                            f"about {BRANCH}")
    if event.workflow not in {wf.name for wf in watched}:
        raise NoVerdict(f"{event.workflow!r} is not one of the watched workflows "
                        f"({sorted(wf.name for wf in watched)}) — the trigger list and "
                        "this event disagree")
    try:
        state = main_state(api, watched)
    except ApiError as err:
        # ⛔ Not "green": that closes the issue while main is red.
        raise NoVerdict(f"cannot tell where {BRANCH} stands — {err}") from err
    try:
        issue = find_issue(api)
        seen = recorded(api, issue) if issue else {}
    except ApiError as err:
        # ⛔ Not "no issue": that opens a duplicate on every run.
        raise NoVerdict(f"cannot tell whether the tracking issue is open, or what it "
                        f"says — {err}") from err
    number = issue["number"] if issue else None
    red = [s for s in state if s.colour == "red"]
    unknown = [s for s in state if s.colour == "unknown"]
    summary = ", ".join(f"{s.wf.name} {s.colour}" for s in state)

    if issue is None:
        if not red:
            return Plan("noop", f"no workflow is red on {BRANCH} ({summary}) and no "
                                "tracking issue is open")
        text = "\n---\n\n".join([_OPEN_HEADER, *(failure_report(api, s) for s in red)])
        rest = _still_lines(unknown, set())
        if rest:
            text += "\n\n---\n\n" + "\n".join(rest)
        return Plan("open", f"red on {BRANCH}: {', '.join(s.wf.name for s in red)}; no "
                            "tracking issue was open", text, red=True)

    if not red and not unknown:
        green = ", ".join(f"`{_code(s.wf.name)}` (`{s.run.sha[:9]}`)" for s in state)
        text = (f"✅ `{BRANCH}` is green again. The newest push run with a verdict of "
                f"every watched workflow is `success`: {green}.")
        return Plan("close", f"every watched workflow is green on {BRANCH}", text, number)

    new_red = [s for s in red
               if seen.get(s.wf.file) != ("red", s.run.run_id, s.run.attempt)]
    recovered = [s for s in state
                 if s.colour == "green" and seen.get(s.wf.file, ("",))[0] == "red"]
    if not new_red and not recovered:
        return Plan("noop", f"{BRANCH} is not all green ({summary}); tracking issue "
                            f"#{number} already says so", issue=number, red=True)
    parts = [failure_report(api, s) for s in new_red]
    for s in recovered:
        parts.append(f"{marker('green', s.wf.file, s.run)}\n**{_code(s.wf.name)}** is green "
                     f"again: `success` at `{s.run.sha[:9]}` (run #{s.run.number}, attempt "
                     f"{s.run.attempt}): {s.run.url}")
    rest = _still_lines(state, {s.wf.file for s in new_red})
    if rest:
        parts.append(f"Not closing: `{BRANCH}` is not green in every watched workflow.\n"
                     + "\n".join(rest))
    return Plan("comment", f"new on {BRANCH}: "
                           + ", ".join([f"{s.wf.name} red" for s in new_red]
                                       + [f"{s.wf.name} green again" for s in recovered]),
                "\n\n---\n\n".join(parts), number, red=True)


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
    return EXIT_RED if action.red else 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
