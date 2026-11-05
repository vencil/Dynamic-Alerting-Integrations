#!/usr/bin/env python3
"""file_race_report.py — dedup + refresh ONE tracking issue per nightly-race failure fingerprint.

Why this exists
===============
`.github/workflows/nightly-race.yaml` called `issues.create()` unconditionally,
from INSIDE a 2-leg matrix job, with a date-stamped title. A persistent failure
therefore minted a fresh P2 issue every night: #1213 (2026-07-24), #1230, #1258,
#1263 — four issues, one bug, and the body never named the failing test so the
duplication was not obvious to a reader.

Three defects, three fixes:
  * blind create              -> look up the existing open issues first (label,
                                 then unlabelled), match by fingerprint marker,
                                 EDIT in place.
  * matrix write race         -> this script runs in a single fan-in `report`
                                 job. Two legs can no longer both find "none"
                                 and both create; the topology, not a lock,
                                 is what makes it impossible.
  * body without evidence     -> the failing tests, first-seen date, and the
                                 raw-stream artifact pointer go in.

Identity = failure fingerprint, not module
------------------------------------------
The first version of this script kept ONE issue per module. That merged
unrelated incidents: #2977 (threshold-exporter) carried a cmd/da-guard package
timeout, a one-night assertion bug in
TestMetrics_CustomAlertDuplicateSeries_DropsTheLaterEntry, and repeated
pkg/parsetree timeouts — one first-seen date (never reset) and a test list
carried over from whichever night last had one. Nothing in that body could say
which of the three was still happening.

Each night's fragment is now split into INCIDENTS (`derive_incidents`), and each
incident has a fingerprint `{module}|{kind}|{subject}`:
  * build_failed            -> `build` (subject: module), and NOTHING else for
                               that module — no test ran, so no other claim holds.
  * each timed-out package  -> `timeout` (subject: the package).
  * a race banner           -> `data-race` (subject: module), listing race_tests.
  * failing tests that are neither race tests nor merely running at a timeout,
    grouped by top-level test -> one `test` incident each (subject: the test)
    when there are at most MAX_TEST_INCIDENTS; more than that -> ONE `tests`
    incident (subject: module) listing them, so a broken shared fixture does not
    open 900 issues.
  * failing, none of the above -> `unattributed` (subject: module).
  * each package in `slow_packages` -> `slow-package` (subject: the package),
    #3541 PR-B: its package-level Elapsed passed SLOW_PACKAGE_RATIO of the
    per-binary -timeout without timing out. WARNING-level: it is tracked like
    the others (filed / refreshed / not seen) but never counts as a failure, so
    it neither suppresses `unattributed` nor turns the module's verdict red.
    A package that timed out is already a `timeout` and is never also slow.
  * fragment missing/unreadable -> `no-result` (subject: module). Kept separate
    from `unattributed`: "the leg never reported" and "the leg reported a
    failure nobody could attribute" are different problems.
The fingerprint is the FIRST line of the issue body as
`<!-- race-fingerprint: … -->` and an existing issue matches only when its body
STARTS with that marker (same rule as bench-attrib-main.yaml): a marker quoted
further down a body — in a pasted log, a comment-turned-edit — cannot capture
another fingerprint's refreshes. The `<!-- race-state: … -->` block (first seen,
streak, last green …) is per issue, i.e. per fingerprint, so first-seen resets
naturally when a different failure starts.

Every night, per module: list the open issues ONCE, map them by fingerprint,
refresh or create one issue per incident, and silently refresh every open
fingerprint issue of that module that did NOT recur ("not seen on <date>",
streak 0). An issue for this module that carries no marker (a legacy per-module
issue such as #2977 / #3343) is never edited: the step summary names it so the
owner can close it by hand. When the night could not measure the other
fingerprints at all (build failure, missing fragment, unattributed failure)
those issues are left untouched instead of being told "not seen".

The label is still called `test-isolation` although it now also covers build,
race and timeout issues: it is the lookup key of every issue already filed, and
renaming it would orphan them.

Notification model (borrowed from scripts/ops/file_cve_report.sh)
------------------------------------------------------------------
The body is refreshed SILENTLY every night and the title carries the live
verdict — GitHub does not notify on a title or body edit. There is deliberately
NO nightly comment and NO auto-close:
  * a comment every morning on a known-failing test is muted within a week
    (that is literally how the 33-night CVE outage stayed invisible), and
  * one quiet night does not mean fixed — this is a REPEAT-RUN detector, so
    close/reopen churn would notify twice per flap and teach the reader nothing.
A night without the fingerprint is recorded as `not seen on <date>` in the body
instead. Streak gating and delta-triggered comments are Tier 2 and deliberately
out of scope here; the state block already carries `streak=` and `last_green=`
so Tier 2 needs no format change.

Wording is driven by EVIDENCE, not by this workflow's name
----------------------------------------------------------
The workflow is called "Nightly Race Detector" and its old body called every
failure a "race detector flake". The failure behind #1213 was neither a race
(`grep -c "DATA RACE"` is 0 across all four nights) nor a flake (9/10, never
10/10). So a title says DATA RACE only when the parser actually saw the race
banner. This is the follow-up the owner asked for in #932 triage.

The same rule applies to test failures. The parser records per-test fail/pass
counts (`test_runs`). A test that failed in EVERY repetition it ran is a plain
failure — a regression or an environment problem — and is described as such;
only a test that failed in SOME repetitions (k of n, k < n) gets isolation
vocabulary and the advice to bisect `-count`. #3343 was titled "repeat-run
failure" for a test that failed all ten repetitions because a binary was
missing. Without counts (an older fragment) the body says the count is unknown.

And one level further down, to the prose and the Triage steps (#3177): a
`-timeout` expiry gets its own verdict ("package timeout") and its own triage
(where did the time go), and a failure nothing could be attributed to says
exactly that. Before this, both shapes were narrated as leaked state between
repetitions, and #2977 sent its reader hunting for state that did not exist.

Fail-closed direction
---------------------
Every unknown resolves toward "worse", never toward "clean":
  * a MISSING or unparseable fragment is a failing module (a leg that died
    before uploading has not proven anything green) AND exits non-zero,
  * an unreadable state block yields no baseline: first-seen is re-stamped but
    explicitly flagged in the body as possibly-earlier, never silently reset,
  * a failed issue lookup is never read as "no existing issue": the alert is
    still filed (a duplicate is visible, a skipped night is silence), the body
    says dedup was unavailable, and the run goes red,
  * a label that cannot be created degrades to unlabelled lookup, still files
    the alert, and exits non-zero so the degradation cannot persist unnoticed.

Usage:
  file_race_report.py --frags-dir DIR --expected-modules a,b --run-url URL
Env:
  REPO                      owner/name (required when DO_FILE=true)
  DO_FILE  (default false)  "true" to actually create/edit issues
  DRY_RUN  (default 0)      "1" to echo gh argv instead of executing (offline tests)
  GITHUB_STEP_SUMMARY       appended to when set
Test seams (DRY_RUN=1 only; unset in CI, where they are inert):
  DRY_RUN_ISSUE_NUM         pretend an open issue with this number exists
  DRY_RUN_PREV_BODY         file holding that issue's current body (its first
                            line decides which fingerprint it is; no marker =
                            a legacy issue)
(There is deliberately no label/delivery failure seam: those paths are driven
by injecting a failing `gh` runner, so the real branches actually execute.)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

# GitHub API hard limits. Documented here because exceeding one 422s from deep
# inside the alerting path, where it reads as "the nightly is broken" rather than
# "the alert never went out" — the #1228 failure mode, 33 nights long.
LABEL_DESC_MAX = 100
ISSUE_TITLE_MAX = 256
# GitHub rejects an issue body over 65536 chars with HTTP 422. This is NOT
# theoretical here: `components/tenant-api` has 933 top-level `func Test*`
# (avg name 34 chars), and each name costs ~40 chars in the rendered list plus
# ~35 in the state block — one broken shared fixture reds the whole package
# under -count=10 and blows the cap at roughly 590 names. The night the issue
# matters most is exactly the night it would fail to post.
ISSUE_BODY_MAX = 65536
# Render at most this many names. Beyond it the list stops being readable
# anyway, and the raw -json artifact carries the complete set.
MAX_RENDERED_TESTS = 200
# Names kept in the machine-readable state block. Smaller than the rendered cap
# because the block is a comparison baseline, not evidence.
MAX_STATE_TESTS = 300
# ...and a CHARACTER budget on top of the count. A count cap alone is not enough:
# 200 pathologically long names still rendered an 80 KB state block, which blew
# the body cap even though the count was under the limit. Caught by the
# boundary test, not by reasoning.
STATE_TESTS_CHARS = 8192
# Up to this many failing top-level tests get one issue each; more collapse into
# a single `tests` issue. A whole package breaking must not open 900 issues.
MAX_TEST_INCIDENTS = 5

# The lookup key. Verified against the repo's existing labels: no collision, and
# `race-flake` (which the old header comment claimed) never existed at all.
LABEL = "test-isolation"
LABEL_COLOR = "D93F0B"
LABEL_DESC = "Nightly go test -race failure, one issue per fingerprint (auto-filed, refreshed in place)"

# Machine-readable state carried inside the issue body. An HTML comment is
# invisible in rendered markdown but survives a body round-trip.
STATE_OPEN = "<!-- race-state:"
STATE_CLOSE = "-->"
STATE_RE = re.compile(re.escape(STATE_OPEN) + r"(.*?)" + re.escape(STATE_CLOSE), re.S)

# Identity marker. ALWAYS the first line of the body, matched at position 0.
FINGERPRINT_OPEN = "<!-- race-fingerprint: "
FINGERPRINT_CLOSE = " -->"
FINGERPRINT_LINE_RE = re.compile(r"^<!-- race-fingerprint: (\S+) -->$")

TITLE_PREFIX_TMPL = "Nightly go test -race — {module}"

INCIDENT_KINDS = ("build", "timeout", "data-race", "test", "tests", "unattributed", "no-result",
                  "slow-package")
# Kinds that are tracked but are NOT failures: a module whose only incidents are
# of these kinds still reads GREEN in the step summary.
WARNING_KINDS = frozenset({"slow-package"})


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _force_utf8_streams() -> None:
    """Make stdout/stderr survive the emoji + em-dashes this script prints.

    CI runners are UTF-8, but a maintainer re-running this on the Windows dev
    host gets cp950 and a UnicodeEncodeError. That crash landed on the DEGRADED
    path specifically (the 🚨 marker only appears when a fragment is missing),
    i.e. the script died exactly when it had bad news to deliver, taking the
    remaining modules' alerts with it. errors="replace" keeps the text readable
    and never trades an alert for a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # detached / already-wrapped stream
                pass


# ── gh plumbing ──────────────────────────────────────────────────────────────


def _subprocess_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False
    )


class Gh:
    """Thin `gh` wrapper with a DRY_RUN echo seam (same shape as file_cve_report.sh).

    `runner` is dependency injection, not test-only cruft: it is how the offline
    suite drives the FAILURE paths (a 422 on create, a 502 on lookup) against the
    real delivery code. The previous design could only be tested through
    environment seams that short-circuited BEFORE the gh call, so the returncode
    handling below had no coverage at all — the defect this rewrite fixes.
    """

    def __init__(self, dry_run: bool, runner=None) -> None:
        self.dry_run = dry_run
        self._runner = runner or _subprocess_runner
        self.calls: list[list[str]] = []
        # Delivery calls that FAILED. Non-empty => the alert did not fully land
        # => the process must exit non-zero. A monitoring job that reports
        # success while its own delivery is broken is the failure mode this
        # entire workflow exists to prevent.
        self.failures: list[str] = []
        # Non-delivery calls that failed (lookups). These do not lose the alert
        # but they DO make dedup untrustworthy, so they must red the run too.
        self.degradations: list[str] = []

    def run(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if self.dry_run:
            print("DRY_RUN gh " + " ".join(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        proc = self._runner(args)
        if proc.stderr and proc.stderr.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        return proc

    def must(self, args: list[str], what: str) -> bool:
        """Run a call whose failure means THE ALERT DID NOT LAND. Returns success.

        Every issue write goes through here. `run()` alone returns a
        CompletedProcess that a caller can (and previously did) ignore, which
        turned a 422 into a cheerful "Filed a new tracking issue." and exit 0.
        """
        proc = self.run(args)
        if proc.returncode != 0:
            self.failures.append(what)
            print(
                f"::error title=Race report::{what} FAILED (gh exited "
                f"{proc.returncode}) — the alert did NOT land. Fix this before "
                "trusting any green night from this workflow."
            )
            return False
        return True


def ensure_label(gh: Gh, repo: str) -> bool:
    """Create/refresh the lookup label. Returns False when it is UNUSABLE.

    Never swallowed with `|| true`: the label is the lookup key, and discarding
    its failure is exactly what produced 33 nights of silence in the CVE path.
    `--force` also EDITS an existing label, so a non-zero exit is ambiguous —
    the label may exist and be perfectly usable and only the edit was rejected.
    Ask the API directly rather than guessing.
    """
    desc = LABEL_DESC
    if len(desc) > LABEL_DESC_MAX:
        print(
            f"::warning title=Race report::label description {len(desc)} > "
            f"{LABEL_DESC_MAX} chars — clamping. Shorten LABEL_DESC in file_race_report.py."
        )
        desc = desc[:LABEL_DESC_MAX]

    # NOTE: there is deliberately no "pretend the label failed" env seam here.
    # The previous one short-circuited ABOVE this line, so the test that claimed
    # to cover the label outage never reached the returncode check or the API
    # probe below — swallowing this whole function still passed. Failure is now
    # injected at the `gh` runner instead, which exercises the real branches.
    proc = gh.run(
        [
            "label", "create", LABEL, "--repo", repo,
            "--color", LABEL_COLOR, "--description", desc, "--force",
        ]
    )
    if proc.returncode == 0:
        return True

    probe = gh.run(["api", f"repos/{repo}/labels/{LABEL}", "--silent"])
    if probe.returncode == 0:
        print(
            f"::warning title=Race report::could not update the '{LABEL}' label "
            "description, but the label exists — continuing."
        )
        return True
    print(
        f"::error title=Race report::label '{LABEL}' is missing and could not be "
        "created. Falling back to the unlabelled lookup so the alert still lands; "
        "fix the label so filtering works again."
    )
    return False


# ── fingerprints ─────────────────────────────────────────────────────────────


def fingerprint(module: str, kind: str, subject: str) -> str:
    # Whitespace and `--` would make the marker unmatchable (the first-line regex
    # wants one token) or end the HTML comment early, and an unmatchable marker
    # means a fresh duplicate every night. Go test names and import paths never
    # contain either; this only defends against an odd fragment.
    subject = re.sub(r"\s+", "_", subject).replace("--", "-_")
    return f"{module}|{kind}|{subject}"


def fingerprint_marker(fp: str) -> str:
    return f"{FINGERPRINT_OPEN}{fp}{FINGERPRINT_CLOSE}"


def body_fingerprint(body: str) -> str | None:
    """The fingerprint an issue body DECLARES — read from its first line only.

    Position 0, not a search: a marker quoted later in a body (a pasted log, an
    earlier body copied into a comment that got edited in) must not let that
    issue capture another fingerprint's nightly refreshes. GitHub may hand the
    body back with CRLF line endings, hence the rstrip.
    """
    first = (body or "").split("\n", 1)[0].rstrip("\r")
    m = FINGERPRINT_LINE_RE.match(first)
    return m.group(1) if m else None


def body_matches(body: str, fp: str) -> bool:
    """True when `body` is the issue for `fp` (marker at position 0)."""
    return body_fingerprint(body) == fp


def split_fingerprint(fp: str) -> tuple[str, str, str]:
    module, _, rest = fp.partition("|")
    kind, _, subject = rest.partition("|")
    return module, kind, subject


def list_open_issues(gh: Gh, repo: str, label_ok: bool, want: set[str]) -> tuple[list[dict], bool]:
    """Open issues that might belong to this module, plus lookup_ok.

    Deliberately NOT `--search`: the search index lags, and a lagging index would
    silently create a duplicate issue every night — the bug being fixed here.
    The label narrows the list; the fingerprint marker selects. The unlabelled
    fallback runs whenever the labelled list did not contain every fingerprint
    seen tonight, not only when the label is broken: a night that filed an
    UNLABELLED issue (label outage) would otherwise be invisible to the next
    night and get a duplicate.

    `lookup_ok=False` means an API call FAILED — which is NOT the same as "no
    matching issue exists". Rows obtained before the failure are still returned
    and used; everything else is treated as unverified by the caller.
    """
    if gh.dry_run:
        num = os.environ.get("DRY_RUN_ISSUE_NUM", "")
        if not num:
            return [], True
        path = os.environ.get("DRY_RUN_PREV_BODY")
        body = Path(path).read_text(encoding="utf-8") if path and Path(path).is_file() else ""
        return [{"number": int(num) if num.isdigit() else num,
                 "title": "(dry run) Nightly go test -race", "body": body}], True

    def _list(args: list[str]) -> list[dict] | None:
        proc = gh.run(args)
        if proc.returncode != 0:
            gh.degradations.append(f"issue lookup (gh exited {proc.returncode})")
            print(
                f"::error title=Race report::issue lookup failed (gh exited "
                f"{proc.returncode}). Cannot tell 'no existing issue' from 'the "
                "API is down', so dedup is NOT trustworthy this run."
            )
            return None
        try:
            rows = json.loads(proc.stdout or "[]")
            if not isinstance(rows, list):
                raise ValueError("not a list")
        except ValueError:
            gh.degradations.append("issue lookup (unparseable JSON)")
            print(
                "::error title=Race report::issue lookup returned unparseable JSON "
                "— treating dedup as unavailable rather than as 'nothing found'."
            )
            return None
        return [r for r in rows if isinstance(r, dict)]

    base = ["issue", "list", "--repo", repo, "--state", "open",
            "--limit", "200", "--json", "number,title,body"]
    rows: list[dict] = []
    if label_ok:
        labelled = _list([*base, "--label", LABEL])
        if labelled is None:
            return [], False
        rows = labelled
        found = {body_fingerprint(str(r.get("body") or "")) for r in rows}
        if want <= found:
            return rows, True
    # Unlabelled fallback. A failure HERE is what makes dedup untrustworthy,
    # because this is the query that would have found an unlabelled issue.
    unlabelled = _list(base)
    if unlabelled is None:
        return rows, False
    seen = {str(r.get("number")) for r in rows}
    rows += [r for r in unlabelled if str(r.get("number")) not in seen]
    return rows, True


# ── state block ──────────────────────────────────────────────────────────────


def parse_state(body: str) -> dict | None:
    """Decode the embedded state block, or None when there is nothing readable.

    None is NOT "clean" — callers treat it as "no baseline", which re-stamps
    first-seen while flagging in the body that the true first occurrence may be
    earlier. Silently resetting first-seen would destroy the one piece of
    evidence (how long this has been failing) the four duplicate issues lost.
    """
    m = STATE_RE.search(body or "")
    if not m:
        return None
    blob = m.group(1)
    state: dict = {}
    tests = re.search(r"tests=\[([^\]]*)\]", blob)
    state["tests"] = sorted({t for t in (tests.group(1).split() if tests else []) if t})
    for key in ("module", "first_seen", "first_run", "last_run", "last_green"):
        km = re.search(rf"\b{key}=(\S*)", blob)
        val = km.group(1) if km else ""
        state[key] = "" if val in ("none", "[]") else val
    # A key absent from an older block reads as 0 — `timed_out` (#3177) did not
    # exist before, and "not recorded" must not make the block unreadable.
    for key in ("streak", "had_data_race", "build_failed", "unattributed",
                "timed_out", "tests_truncated"):
        km = re.search(rf"\b{key}=(\d+)", blob)
        state[key] = int(km.group(1)) if km else 0
    return state


def render_state(state: dict) -> str:
    """Serialise the state block, bounded so it can never blow the body cap.

    The tests list is capped and the truncation is RECORDED (`tests_truncated=1`)
    rather than silently applied: a baseline that quietly lost entries would make
    the next night's comparison read "new failing tests" for tests that were
    there all along.
    """
    all_tests = list(state["tests"])
    truncated = bool(state.get("tests_truncated")) or len(all_tests) > MAX_STATE_TESTS
    # Bound by COUNT and by CHARACTERS — see STATE_TESTS_CHARS.
    tests: list[str] = []
    used = 0
    for name in all_tests[:MAX_STATE_TESTS]:
        if used + len(name) + 1 > STATE_TESTS_CHARS:
            truncated = True
            break
        tests.append(name)
        used += len(name) + 1
    truncated = int(truncated)
    return (
        f"{STATE_OPEN} module={state['module']} "
        f"first_seen={state['first_seen'] or 'none'} "
        f"first_run={state['first_run'] or 'none'} "
        f"last_run={state['last_run'] or 'none'} "
        f"last_green={state['last_green'] or 'none'} "
        f"streak={state['streak']} "
        f"had_data_race={int(bool(state['had_data_race']))} "
        f"build_failed={int(bool(state['build_failed']))} "
        f"unattributed={int(bool(state['unattributed']))} "
        f"timed_out={int(bool(state.get('timed_out')))} "
        f"tests_truncated={truncated} "
        f"tests=[{' '.join(tests)}] {STATE_CLOSE}"
    )


# ── fragments ────────────────────────────────────────────────────────────────


def load_fragment(frags_dir: Path, module: str) -> tuple[dict, str]:
    """Fragment for `module`, plus a degradation note ("" when healthy).

    A leg that never uploaded has NOT proven itself green, so a missing or
    corrupt fragment synthesises a FAILING one. The alternative — skipping the
    module — is the silent-clean direction this whole rework exists to remove.
    """
    path = frags_dir / f"frag-{module}.json"
    stub = {
        "module": module, "failed": True, "failed_tests": [], "race_tests": [],
        "had_data_race": False, "build_failed": False, "unattributed_failure": True,
        "timed_out": False, "timeout_packages": [], "timeout_running_tests": [],
        "slow_packages": [], "go_exit_code": -1, "fail_events": 0, "non_json_lines": 0, "total_events": 0,
        "raw_missing": True, "run_url": "", "workdir": "", "packages": "",
        "fragment_missing": True,
    }
    if not path.is_file():
        return stub, (f"fragment for `{module}` is missing — its merge leg never uploaded "
                      "one (a race shard left no complete evidence, or the leg never ran)")
    try:
        frag = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(frag, dict):
            raise ValueError("fragment is not an object")
    except (ValueError, OSError) as exc:
        return stub, f"fragment for `{module}` is unreadable ({exc})"
    frag.setdefault("module", module)
    frag["fragment_missing"] = False
    return frag, ""


# ── incidents ────────────────────────────────────────────────────────────────


def _root(name: str) -> str:
    """Top-level test of a (sub)test name: `TestA/b/c` -> `TestA`."""
    return name.split("/", 1)[0]


def _str_list(value) -> list[str]:
    return [v.strip() for v in (value or []) if isinstance(v, str) and v.strip()]


def derive_incidents(frag: dict) -> list[dict]:
    """Split one module's fragment into independently tracked incidents.

    Rules (the module docstring explains why each exists):
      build subsumes everything; one `timeout` per package; one `data-race` per
      module; failing top-level tests that are not race tests and not merely
      running at a timeout -> `test` each, or one `tests` above
      MAX_TEST_INCIDENTS; nothing attributable -> `unattributed`; no fragment ->
      `no-result`; then one warning-level `slow-package` per slow package. A
      green fragment without slow packages yields [].
    """
    module = frag["module"]

    def inc(kind: str, subject: str, **extra) -> dict:
        return {"module": module, "kind": kind, "subject": subject,
                "fingerprint": fingerprint(module, kind, subject), **extra}

    if frag.get("fragment_missing"):
        return [inc("no-result", module)]
    if frag.get("build_failed"):
        return [inc("build", module)]

    out: list[dict] = []
    timeouts = [tp for tp in (frag.get("timeout_packages") or []) if isinstance(tp, dict)]
    running: set[str] = set(_str_list(frag.get("timeout_running_tests")))
    for tp in timeouts:
        running |= set(_str_list(tp.get("running_tests")))
        pkg = str(tp.get("package") or "").strip() or "unknown-package"
        out.append(inc("timeout", pkg, timeout=tp, tests=sorted(_str_list(tp.get("running_tests")))))
    if frag.get("timed_out") and not timeouts:
        # An older or partial fragment: the timeout is known, the package is not.
        out.append(inc("timeout", "unknown-package", timeout={}, tests=[]))

    race_tests = sorted(set(_str_list(frag.get("race_tests"))))
    if frag.get("had_data_race"):
        out.append(inc("data-race", module, tests=race_tests))
    race_roots = {_root(t) for t in race_tests}
    running_roots = {_root(t) for t in running}

    groups: dict[str, list[str]] = {}
    for name in _str_list(frag.get("failed_tests")):
        groups.setdefault(_root(name), []).append(name)
    test_groups: dict[str, list[str]] = {}
    for root, names in groups.items():
        if root in race_roots:
            continue  # tracked by the data-race incident
        # "Merely running at the timeout": every failing name of this test is
        # one the panic listed as running (or the parent of one). A test that
        # ALSO failed on its own keeps its own incident.
        if root in running_roots and all(n in running or n == root for n in names):
            continue
        test_groups[root] = sorted(set(names))

    runs = frag.get("test_runs") if isinstance(frag.get("test_runs"), dict) else None
    if len(test_groups) > MAX_TEST_INCIDENTS:
        out.append(inc("tests", module, tests=sorted(test_groups), groups=test_groups, runs=runs))
    else:
        for root in sorted(test_groups):
            out.append(inc("test", root, tests=test_groups[root], runs=runs))

    if not out and frag.get("failed"):
        out.append(inc("unattributed", module))

    # Warning-level, appended AFTER the unattributed check: a slow package is
    # not an explanation for a failing exit code.
    timed_out_pkgs = {str(tp.get("package") or "").strip() for tp in timeouts}
    try:
        threshold = float(frag.get("slow_package_threshold_s") or 0)
        budget = float(frag.get("go_test_timeout_s") or 0)
    except (TypeError, ValueError):
        threshold, budget = 0.0, 0.0
    for sp in frag.get("slow_packages") or []:
        if not isinstance(sp, dict):
            continue
        pkg = str(sp.get("package") or "").strip()
        if not pkg or pkg in timed_out_pkgs:
            continue
        try:
            elapsed = float(sp.get("elapsed_s"))
        except (TypeError, ValueError):
            continue
        out.append(inc("slow-package", pkg, tests=[], elapsed_s=elapsed,
                       threshold_s=threshold, timeout_s=budget))
    return out


def _secs(value) -> str:
    """`954.8` -> `954.8 s`, `1800.0` -> `1800 s`; `?` when unknown."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if v <= 0:
        return "?"
    return f"{v:g} s" if v == int(v) else f"{v:.1f} s"


def repetitions(runs: dict | None, test: str) -> tuple[str, int, int]:
    """("every" | "some" | "unknown", failed, ran) for one top-level test.

    "unknown" whenever the parser did not record counts for this name — the
    report then says so instead of picking a vocabulary.
    """
    entry = (runs or {}).get(test) if isinstance(runs, dict) else None
    if not isinstance(entry, dict):
        return "unknown", 0, 0
    try:
        k, ok = int(entry.get("fail", 0)), int(entry.get("pass", 0))
    except (TypeError, ValueError):
        return "unknown", 0, 0
    if k <= 0:
        return "unknown", 0, 0
    return ("every" if ok == 0 else "some"), k, k + ok


def _rep_note(shape: str, k: int, n: int) -> str:
    if shape == "every":
        return f"failed every repetition ({k}/{n})"
    if shape == "some":
        return f"failed {k} of {n} repetitions"
    return "repetition count not recorded"


# ── composition ──────────────────────────────────────────────────────────────


def incident_verdict(inc: dict) -> str:
    kind = inc["kind"]
    if kind == "no-result":
        return "NO RESULT — leg produced no fragment"
    if kind == "build":
        return "build failure"
    if kind == "data-race":
        n = len(inc.get("tests") or [])
        return f"DATA RACE ({n} test(s))" if n else "DATA RACE"
    if kind == "timeout":
        after = (inc.get("timeout") or {}).get("after")
        return f"package timeout after {after}" if after else "package timeout"
    if kind == "test":
        shape, k, n = repetitions(inc.get("runs"), inc["subject"])
        if shape == "every":
            return f"fails every repetition ({k}/{n})"
        if shape == "some":
            return f"isolation-shaped — failed {k} of {n} repetitions"
        return "failing (repetition count not recorded)"
    if kind == "tests":
        names = inc.get("tests") or []
        shapes = [repetitions(inc.get("runs"), t)[0] for t in names]
        if shapes and all(s == "every" for s in shapes):
            return f"{len(names)} tests fail every repetition"
        if shapes and all(s == "some" for s in shapes):
            return f"{len(names)} tests failing, isolation-shaped"
        return f"{len(names)} tests failing"
    if kind == "unattributed":
        return "failing, cause NOT attributed"
    if kind == "slow-package":
        return (f"WARNING, not failing — {_secs(inc.get('elapsed_s'))} of the "
                f"{_secs(inc.get('timeout_s'))} -timeout")
    return "failing"


def make_title(module: str, kind: str, subject: str, verdict: str) -> str:
    """`… — {module} — {kind}: {subject} — {verdict}`, clamped to GitHub's cap.

    The SUBJECT is what gets shortened, never the verdict: the title is where
    the live state is read, and a package path is the one part that can be long.
    """
    prefix = TITLE_PREFIX_TMPL.format(module=module)
    head, tail = f"{prefix} — {kind}: ", f" — {verdict}"
    room = ISSUE_TITLE_MAX - len(head) - len(tail)
    if len(subject) > room:
        subject = subject[:max(0, room - 1)] + "…"
    return clamp_title(f"{head}{subject}{tail}")


ISOLATION_CLAIM_TEXT = (
    "This is a repeat-run / test-isolation failure: the test passes in some "
    "repetitions and fails in others when the suite runs 10x in one process, which "
    "usually means leaked state between repetitions (package-level vars, shared temp "
    "dirs, a singleton, an unclosed goroutine) rather than a concurrency bug. Do not "
    "describe it as a race in the fix."
)
ISOLATION_STEP_TEXT = ("Bisect the repetition count (`-count=2`, `-count=5`) to find where "
                       "isolation breaks.")


def _test_lines(inc: dict, names: list[str]) -> list[str]:
    runs = inc.get("runs")
    lines = []
    for t in names[:MAX_RENDERED_TESTS]:
        note = _rep_note(*repetitions(runs, _root(t))) if t == _root(t) else "subtest"
        lines.append(f"- `{t}` — {note}")
    return lines


def compose_body(inc: dict, frag: dict, state: dict, run_url: str, date: str,
                 baseline_readable: bool, degraded: str, lookup_ok: bool = True,
                 seen: bool = True, caveat: str = "") -> str:
    """Body for one fingerprint issue. `seen=False` is the not-seen-tonight refresh."""
    module, kind, subject = inc["module"], inc["kind"], inc["subject"]
    fp = inc["fingerprint"]
    verdict = incident_verdict(inc) if seen else f"not seen on {date}"
    warn = []
    if degraded:
        warn.append(f"🚨 **Degraded report** — {degraded}. Treat this module as UNVERIFIED, not clean.")
    if not lookup_ok:
        warn.append(
            "🚨 **Dedup was unavailable** — the existing-issue lookup failed, so this "
            "issue was filed WITHOUT being able to confirm no duplicate exists. "
            "Check for an older open issue with the same fingerprint and close "
            "whichever is redundant. The alert is delivered loudly rather than "
            "skipped: a missed first night is worse than a duplicate you can see."
        )
    if not baseline_readable and state.get("_had_issue"):
        warn.append(
            "⚠️ The previous state block was unreadable, so **first-seen was re-stamped** "
            "— the real first occurrence may be earlier than shown below."
        )
    if seen and kind == "unattributed":
        warn.append(
            "⚠️ `go test` exited non-zero but no failing test, build failure, race "
            "banner, or test-timeout panic could be attributed. Read the raw stream "
            "artifact before assuming anything — this is the state the old workflow reported as if it were informative."
        )
    if seen and frag.get("non_json_lines"):
        warn.append(f"ℹ️ {frag['non_json_lines']} non-JSON line(s) in the stream were skipped.")

    run_filter = f" -run '^{subject}$'" if kind == "test" else ""
    # A slow package is one binary: reproduce just it, with the nightly's flags.
    targets = subject if kind == "slow-package" else (frag.get("packages") or "./...")
    extra = " -short -json" if kind == "slow-package" else ""
    reproduce = (
        f"cd {frag.get('workdir') or '<workdir>'} && "
        f"go test {targets} -race -count=10{extra}{run_filter}"
    )
    first_run = state.get("first_run") or run_url
    rows = [
        "| | |", "|---|---|",
        f"| Verdict | {verdict} |",
        f"| Kind | `{kind}` |",
        f"| Subject | `{subject}` |",
        f"| First seen | {state.get('first_seen') or date} ([run]({first_run})) |",
        f"| Latest run | {date} ([run]({run_url})) |",
        f"| Last night not seen | {state.get('last_green') or 'never recorded'} |",
        f"| Consecutive {'nights seen' if kind in WARNING_KINDS else 'failing nights'} "
        f"| {state.get('streak', 0)} |",
    ]

    out = [
        fingerprint_marker(fp),
        f"Nightly `go test -race -count=10` tracking issue for **{module}** on `main`, "
        f"one issue per failure fingerprint: `{fp}`.",
        "",
    ]
    if warn:
        out += warn + [""]
    out += rows + [""]

    nature: list[str] = []
    steps: list[str] = []
    tests = list(inc.get("tests") or [])

    if not seen:
        out += [f"## Not seen on {date}", "",
                "The latest run did **not** reproduce this fingerprint. That is not proof "
                "it is fixed — this is a repeat-run detector, and one quiet night is one "
                "sample. Close the issue by hand once the fix has landed."
                + (f" {caveat}" if caveat else ""), ""]
        if tests:
            out += ["Last recorded evidence (from the state block):", ""]
            out += [f"- `{t}`" for t in tests[:MAX_RENDERED_TESTS]] + [""]
        steps.append("If a fix landed, link it here and close this issue; if not, keep it "
                     "open — the next failing night refreshes it in place.")
    elif kind == "test":
        shape, k, n = repetitions(inc.get("runs"), subject)
        out += [f"## Failing test `{subject}`", ""] + _test_lines(inc, tests) + [""]
        if shape == "every":
            nature.append(
                f"**Fails in every repetition it ran ({k} of {n}).** This is a plain "
                "failure — a regression, or an environment problem such as a missing "
                "binary, tool, file or network dependency — NOT leaked state between "
                "repetitions. Do not bisect `-count` to look for an isolation bug."
            )
            steps.append(
                f"Run it once (`-count=1 -run '^{subject}$'`): a test that fails every "
                "repetition fails on the first one too. Read its first failure message in "
                "the artifact."
            )
            steps.append("Check the environment first (binaries and tools the test shells "
                         "out to), then the most recent change to the code under test.")
        elif shape == "some":
            nature.append(f"**Failed in {k} of {n} repetitions; no data race was attributed "
                          f"to it.** {ISOLATION_CLAIM_TEXT}")
            steps.append(ISOLATION_STEP_TEXT)
        else:
            nature.append(
                "**Repetition counts were not recorded for this test**, so this report "
                "cannot tell a plain failure (fails every repetition) from an "
                "isolation-shaped one (fails in some). No data race was attributed to it."
            )
            steps.append(f"Count this test's `pass` and `fail` events in the `race-raw-{module}-<shard>` "
                         "artifacts: failing every repetition means a plain failure, failing in "
                         "some means isolation.")
    elif kind == "tests":
        groups = inc.get("groups") or {}
        shown = tests[:MAX_RENDERED_TESTS]
        out += [
            f"## Failing tests ({len(tests)} distinct top-level tests, from "
            f"{frag.get('fail_events', 0)} fail events across 10 repetitions)",
            "",
        ] + _test_lines(inc, shown)
        if len(tests) > len(shown):
            out += [
                "",
                f"_… and {len(tests) - len(shown)} more (list capped at "
                f"{MAX_RENDERED_TESTS} to stay inside GitHub's {ISSUE_BODY_MAX}-char "
                f"body limit) — the complete list is in the `race-raw-{module}-<shard>` artifacts._",
            ]
        out += [""]
        shapes = [repetitions(inc.get("runs"), t)[0] for t in tests]
        every, some = shapes.count("every"), shapes.count("some")
        nature.append(
            f"**{len(tests)} top-level tests failed** — more than {MAX_TEST_INCIDENTS}, so "
            "they share this one issue instead of one each. A count this large usually "
            f"means one shared fixture or setup step broke many tests, not {len(tests)} "
            "separate bugs."
        )
        nature.append(
            f"{every} failed in every repetition they ran (plain failures, not leaked "
            f"state); {some} failed in only some repetitions (isolation-shaped); "
            f"{len(tests) - every - some} without recorded counts."
        )
        steps.append("Find the FIRST failure in the artifact: a broken shared fixture "
                     "usually fails every test after it.")
        if sum(len(v) for v in groups.values()) > len(tests):
            steps.append("Failing subtests are listed in the artifact; this body lists "
                         "top-level tests only.")
    elif kind == "timeout":
        tp = inc.get("timeout") or {}
        running = list(tp.get("running_tests") or [])
        out += ["## Package timeout", ""]
        if not tp:
            out += ["_The timed-out package could not be identified — read the raw "
                    "stream artifact._", ""]
        else:
            out += [f"`{tp.get('package') or '<unknown package>'}` hit "
                    f"`-timeout {tp.get('after') or '?'}`. Tests still running at the "
                    f"deadline ({len(running)}):", ""]
            shown = running[:MAX_RENDERED_TESTS]
            out += [f"- `{t}`" for t in shown] or ["- _none listed in the panic_"]
            if len(running) > len(shown):
                out += ["", f"_… and {len(running) - len(shown)} more — see the "
                            f"`race-raw-{module}-<shard>` artifacts._"]
            out += [""]
        out += ["_Running at the deadline is not the same as failing: these tests were "
                "in flight when the clock ran out, which makes them the first ones to "
                "measure, not necessarily the slow ones._", ""]
        para = (
            f"**Package timeout.** `{subject}` ran past `go test`'s `-timeout` and the "
            "test binary was killed with `panic: test timed out`. The timeout itself is a "
            "time budget being exceeded, not a failing assertion."
        )
        # Only when no test failed anywhere in the module tonight: otherwise this
        # sentence would contradict the test issues filed alongside it.
        if not _str_list(frag.get("failed_tests")):
            para += (" No test reported a failure, so it is NOT evidence of leaked state "
                     "between repetitions. Do not bisect `-count` to look for an "
                     "isolation bug.")
        else:
            para += (" Tests that did fail tonight are tracked in their own fingerprint "
                     "issues.")
        nature.append(para)
        steps.append(
            f"Find where the time went: in the `race-raw-{module}-<shard>` artifacts, compare "
            "the package's `Elapsed` with the `-timeout` above, and rank that package's "
            "`pass`/`fail` events by `Elapsed`. Start with the tests listed as running "
            "at the deadline."
        )
    elif kind == "slow-package":
        elapsed, budget = inc.get("elapsed_s"), inc.get("timeout_s")
        out += ["## Slow package", "",
                f"`{subject}` took **{_secs(elapsed)}** (its package-level `Elapsed`) "
                f"under `-race -count=10 -short`. Threshold: {_secs(inc.get('threshold_s'))}, "
                f"i.e. more than half of the {_secs(budget)} per-binary `-timeout`.", ""]
        nature.append(
            "**A warning, not a failure.** The package finished inside its `-timeout`; "
            "any test that failed tonight is tracked in its own issue. This one only says "
            "the package is spending most of its time budget, and it does not turn the "
            "module's nightly verdict red."
        )
        nature.append(
            "**Sharding cannot fix it.** A package is ONE test binary, and `-timeout` is "
            "a budget per binary: the nightly can put this package on a shard of its own, "
            "but it cannot split it. If it keeps growing it becomes a `timeout` incident "
            "(the binary is killed with `panic: test timed out`). The only fix is making "
            "the package's tests cheaper — raising `-timeout` just postpones the collision."
        )
        steps.append(
            f"Profile the heaviest tests from the `race-raw-{module}-<shard>` artifact of "
            "the shard that ran this package: take its top-level `pass`/`fail` events "
            f"(`Package` = `{subject}`, `Test` without a `/`; one event per repetition), "
            "sum each test's `Elapsed` over its 10 repetitions and rank the tests by it."
        )
        steps.append(
            "Cut the cost of the top few (a cheaper fixture, `testing.Short()` for an "
            "exhaustive matrix that PR CI already runs in full, or `t.Parallel()` where "
            "the test is independent), then compare the next nightly's `Elapsed` here."
        )
    elif kind == "data-race":
        if tests:
            out += ["**Race banner attributed to:** " + ", ".join(f"`{t}`" for t in tests), ""]
        nature.append("A real data race was detected. This is a concurrency bug, not a flake — "
                      "the report below is reproducible evidence, not a sampling artifact.")
        steps.append("Read the race report in the artifact: its two stacks name the "
                     "conflicting accesses.")
    elif kind == "build":
        nature.append("The package failed to BUILD under `-race`. No test result exists for "
                      "this run, so no other fingerprint of this module was evaluated tonight.")
        steps.append("Read the build error in the artifact; no test ran.")
    else:  # unattributed / no-result: say what the warning says, nothing more.
        nature.append(
            "**Cause NOT attributed.** `go test` failed, but nothing in the stream "
            "identified why — no failing test, build failure, race banner, or timeout "
            "panic. This report does not know whether it is a race, a timeout, or an "
            "isolation problem; read the raw stream before assuming any of them."
        )
        steps.append("Read the raw stream artifact for a `panic:`, a signal, or a `FAIL` "
                     "line, and attribute the cause before choosing a fix.")

    if nature:
        out += ["## What this is", ""]
        for para in nature:
            out += [para, ""]

    triage = [f"Reproduce: `{reproduce}`", *steps,
              f"Full evidence — race report / panic / build error — is in the "
              f"`race-raw-{module}-<shard>` artifacts on [the run]({run_url})."]
    out += ["## Triage", ""]
    out += [f"{i}. {step}" for i, step in enumerate(triage, 1)]
    out += [
        "",
        "This issue tracks ONE failure fingerprint; other failures of this module are "
        "tracked in their own issues. It is **refreshed in place** every night (silent "
        "body edit — GitHub does not notify on title/body edits), including nights it "
        "does not recur. It is deliberately NOT auto-closed: one quiet night does not "
        "prove a nightly failure is fixed. Close it by hand once the fix lands.",
        "",
        "Auto-filed by [`.github/workflows/nightly-race.yaml`]"
        "(https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/workflows/nightly-race.yaml).",
        "",
    ]
    # The state block is appended AFTER clamping, never clamped itself: a
    # half-truncated `<!-- race-state: ... ` would be unparseable, and an
    # unparseable baseline makes the next night re-stamp first-seen and lose the
    # evidence of when this started. Prose is what gets cut; state survives —
    # and so does the fingerprint marker, because truncation cuts from the END.
    return _clamp_body("\n".join(out), render_state(state))


def _clamp_body(prose: str, state_marker: str) -> str:
    """Fit prose + state marker inside GitHub's body cap, truncation marked.

    Backstop only — MAX_RENDERED_TESTS should keep normal bodies far under the
    cap. It exists because the alternative to a visible truncation notice is an
    HTTP 422 on the night the issue matters most.
    """
    note = (
        "\n\n_⚠️ Body truncated to fit GitHub's "
        f"{ISSUE_BODY_MAX}-character issue limit — see the raw artifact for the "
        "complete output._"
    )
    # max(0, ...) is defensive: render_state bounds the marker, but a negative
    # budget would make `prose[:budget]` silently slice from the END and ship a
    # body that is both wrong and still over the cap.
    budget = max(0, ISSUE_BODY_MAX - len(state_marker) - len(note) - 2)
    if len(prose) > budget:
        prose = prose[:budget] + note
    return prose + "\n" + state_marker


def clamp_title(title: str) -> str:
    return title if len(title) <= ISSUE_TITLE_MAX else title[:ISSUE_TITLE_MAX]


# ── per-module driver ────────────────────────────────────────────────────────


def _is_legacy_for(row: dict, module: str) -> bool:
    """A pre-fingerprint per-module issue: right title prefix, no marker."""
    prefix = TITLE_PREFIX_TMPL.format(module=module)
    title = str(row.get("title") or "")
    if not (title == prefix or title.startswith(prefix + " —")):
        return False
    return body_fingerprint(str(row.get("body") or "")) is None


def _incident_state(inc: dict, prev: dict, run_url: str, date: str, had_issue: bool) -> dict:
    kind = inc["kind"]
    return {
        "module": inc["module"],
        "first_seen": prev.get("first_seen") or date,
        # PRESERVE the first-seen run URL across refreshes: it is the only record
        # of when this started. Per fingerprint, so a DIFFERENT failure gets its
        # own first-seen instead of inheriting an unrelated one (#2977).
        "first_run": prev.get("first_run") or run_url,
        "last_run": run_url,
        "last_green": prev.get("last_green", ""),
        "streak": int(prev.get("streak", 0)) + 1,
        "had_data_race": kind == "data-race",
        "build_failed": kind == "build",
        "unattributed": kind in ("unattributed", "no-result"),
        "timed_out": kind == "timeout",
        # Tonight's evidence only — never carried over from another night.
        "tests": list(inc.get("tests") or []),
        "_had_issue": had_issue,
    }


def handle_module(gh: Gh, repo: str, module: str, frag: dict, degraded: str,
                  run_url: str, do_file: bool, label_ok: bool) -> str:
    """Files/refreshes this module's fingerprint issues; returns step-summary markdown."""
    date = today()
    incidents = derive_incidents(frag)
    tonight = {i["fingerprint"] for i in incidents}
    # Nights that could not measure the OTHER fingerprints at all. Telling their
    # issues "not seen" would be the silent-clean direction.
    inconclusive = bool(frag.get("fragment_missing") or frag.get("build_failed")
                        or any(i["kind"] == "unattributed" for i in incidents))

    # The verdict counts FAILURES only; warning-level incidents (slow-package)
    # are listed under a module that still reads GREEN.
    failures = [i for i in incidents if i["kind"] not in WARNING_KINDS]
    warnings = len(incidents) - len(failures)
    note = f" — {warnings} warning(s)" if warnings else ""
    head = (f"### {module} — {len(failures)} incident(s){note}" if failures
            else f"### {module} — GREEN ({date}){note}")
    summary = [head]
    if degraded:
        summary.append(f"🚨 {degraded}")
    for inc in incidents:
        summary.append(f"- `{inc['kind']}: {inc['subject']}` — {incident_verdict(inc)}")

    if not do_file:
        summary.append("_(summary only — DO_FILE!=true, issues untouched)_")
        return "\n".join(summary)

    rows, lookup_ok = list_open_issues(gh, repo, label_ok, tonight)
    by_fp: dict[str, dict] = {}
    duplicates: list[str] = []
    legacy: list[str] = []
    for row in sorted(rows, key=lambda r: int(r["number"]) if str(r.get("number")).isdigit() else 0):
        fp = body_fingerprint(str(row.get("body") or ""))
        if fp is None:
            if _is_legacy_for(row, module):
                legacy.append(f"#{row.get('number')}")
            continue
        if split_fingerprint(fp)[0] != module:
            continue
        if fp in by_fp:
            duplicates.append(f"#{row.get('number')} (same fingerprint as #{by_fp[fp].get('number')})")
            continue
        by_fp[fp] = row  # the OLDEST open issue for a fingerprint is canonical

    labels = ["bug", "ci", "P2"] + ([LABEL] if label_ok else [])
    for inc in incidents:
        fp = inc["fingerprint"]
        row = by_fp.get(fp)
        num = str(row.get("number")) if row else ""
        prev = parse_state(str(row.get("body") or "")) if row else None
        baseline_readable = prev is not None
        state = _incident_state(inc, prev or {}, run_url, date, bool(num))
        verdict = incident_verdict(inc)
        title = make_title(module, inc["kind"], inc["subject"], verdict)
        body = compose_body(inc, frag, state, run_url, date, baseline_readable, degraded,
                            lookup_ok=lookup_ok or bool(num))
        if num:
            # EDIT, never comment: a nightly comment on a known-failing test is
            # muted within a week, and a muted alert is the same as no alert.
            print(f"refreshing #{num} for {fp} (silent body edit)")
            ok = gh.must(
                ["issue", "edit", num, "--repo", repo, "--title", title, "--body", body],
                f"refreshing issue #{num} for {fp}",
            )
            summary.append(
                f"  - Refreshed issue #{num} (silent)." if ok
                else f"  - ❌ FAILED to refresh issue #{num} — the alert did NOT land."
            )
        else:
            # Dedup could not be confirmed -> file anyway. A duplicate is visible
            # and closable; a skipped first night is silence, which is the failure
            # mode this workflow exists to remove. The run still goes red.
            print(f"filing new tracking issue for {fp}"
                  + ("" if lookup_ok else " (WITHOUT a trustworthy dedup lookup)"))
            args = ["issue", "create", "--repo", repo, "--title", title,
                    "--assignee", "vencil", "--body", body]
            for lab in labels:
                args += ["--label", lab]
            ok = gh.must(args, f"filing the tracking issue for {fp}")
            summary.append(
                "  - Filed a new tracking issue." if ok
                else "  - ❌ FAILED to file the tracking issue — the alert did NOT land."
            )

    unseen = [(fp, row) for fp, row in by_fp.items() if fp not in tonight]
    if unseen and inconclusive:
        summary.append(
            "Not re-evaluated tonight (this run could not measure them): "
            + ", ".join(f"#{row.get('number')}" for _, row in unseen)
        )
    elif unseen:
        caveat = ""
        if any(i["kind"] == "timeout" for i in incidents):
            caveat = ("Tonight's run also hit a package timeout, so tests scheduled after "
                      "the deadline did not run — absence is weaker evidence than usual.")
        timed_out_tonight = {i["subject"] for i in incidents if i["kind"] == "timeout"}
        for fp, row in unseen:
            num = str(row.get("number"))
            prev = parse_state(str(row.get("body") or ""))
            p = prev or {}
            _, kind, subject = split_fingerprint(fp)
            this_caveat = caveat
            if kind == "slow-package" and subject in timed_out_tonight:
                # Absent because it got WORSE: say so instead of reading as relief.
                this_caveat = ("This package hit the `-timeout` tonight — it is tracked "
                               "by its `timeout` issue now, not because it got faster.")
            inc = {"module": module, "kind": kind, "subject": subject, "fingerprint": fp,
                   "tests": p.get("tests", [])}
            state = {
                "module": module,
                "first_seen": p.get("first_seen", ""),
                "first_run": p.get("first_run", ""),
                "last_run": run_url,
                "last_green": date,
                "streak": 0,
                "had_data_race": p.get("had_data_race", 0),
                "build_failed": p.get("build_failed", 0),
                "unattributed": p.get("unattributed", 0),
                "timed_out": p.get("timed_out", 0),
                "tests": p.get("tests", []),
                "tests_truncated": p.get("tests_truncated", 0),
                "_had_issue": True,
            }
            title = make_title(module, kind, subject, f"not seen on {date}")
            body = compose_body(inc, frag, state, run_url, date, prev is not None, "",
                                seen=False, caveat=this_caveat)
            print(f"refreshing #{num} for {fp} (not seen tonight, silent body edit)")
            ok = gh.must(
                ["issue", "edit", num, "--repo", repo, "--title", title, "--body", body],
                f"refreshing issue #{num} for {fp} (not seen)",
            )
            summary.append(
                f"- `{kind}: {subject}` — not seen tonight → Refreshed issue #{num} (silent)."
                if ok else f"- ❌ FAILED to refresh issue #{num} (not seen) — the alert did NOT land."
            )

    if not incidents and not by_fp and lookup_ok:
        summary.append("_(green, no open issue — nothing to do)_")
    if not lookup_ok:
        summary.append("_(the issue lookup failed — dedup unverified this run)_")
    if duplicates:
        summary.append("Duplicate fingerprint issues (close by hand): " + ", ".join(duplicates))
    if legacy:
        summary.append(
            "Legacy per-module issue(s) with no fingerprint marker, left untouched — "
            "close by hand once the fingerprint issues cover them: " + ", ".join(legacy)
        )
    return "\n".join(summary)


def main(argv: list[str] | None = None, runner=None) -> int:
    _force_utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frags-dir", required=True, type=Path)
    ap.add_argument("--expected-modules", required=True,
                    help="comma-separated module names; MUST equal the race matrix")
    ap.add_argument("--run-url", default="")
    args = ap.parse_args(argv)

    modules = [m.strip() for m in args.expected_modules.split(",") if m.strip()]
    if not modules:
        print("::error title=Race report::--expected-modules is empty", file=sys.stderr)
        return 2

    do_file = os.environ.get("DO_FILE", "false") == "true"
    dry_run = os.environ.get("DRY_RUN", "0") == "1"
    repo = os.environ.get("REPO", "")
    if do_file and not repo:
        print("::error title=Race report::REPO is required when DO_FILE=true", file=sys.stderr)
        return 2

    gh = Gh(dry_run, runner=runner)
    label_ok = True
    if do_file:
        label_ok = ensure_label(gh, repo)

    lines = [f"# Nightly Race Detector — {today()}", ""]
    ok = True
    for module in modules:
        frag, degraded = load_fragment(args.frags_dir, module)
        if degraded:
            ok = False
            print(f"::error title=Race report::{degraded}")
        lines += [handle_module(gh, repo, module, frag, degraded,
                                args.run_url, do_file, label_ok), ""]

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    if gh.failures:
        # The loudest failure class: we had something to say and could not say
        # it. Never let this exit 0 — a monitoring job that reports success
        # while its own delivery is broken is strictly worse than one that is
        # merely red, because nobody goes looking.
        print(
            "::error title=Race report::"
            f"{len(gh.failures)} issue write(s) FAILED: {'; '.join(gh.failures)}. "
            "The tracking issues do NOT reflect this run."
        )
        ok = False
    if gh.degradations:
        print(
            "::error title=Race report::dedup was NOT trustworthy this run: "
            f"{'; '.join(gh.degradations)}. Check for duplicate issues."
        )
        ok = False
    if not label_ok:
        print(
            f"::error title=Race report::alert delivered WITHOUT the '{LABEL}' label — "
            "lookup is degraded and WILL duplicate issues until the label is fixed."
        )
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
