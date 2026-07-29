#!/usr/bin/env python3
"""file_race_report.py — dedup + refresh ONE tracking issue per nightly-race module.

Why this exists
===============
`.github/workflows/nightly-race.yaml` called `issues.create()` unconditionally,
from INSIDE a 2-leg matrix job, with a date-stamped title. A persistent failure
therefore minted a fresh P2 issue every night: #1213 (2026-07-24), #1230, #1258,
#1263 — four issues, one bug, and the body never named the failing test so the
duplication was not obvious to a reader.

Three defects, three fixes:
  * blind create              -> look up an existing open issue first (label,
                                 then per-module title PREFIX), EDIT in place.
  * matrix write race         -> this script runs in a single fan-in `report`
                                 job. Two legs can no longer both find "none"
                                 and both create; the topology, not a lock,
                                 is what makes it impossible.
  * body without evidence     -> the deduped failing-test list, first-seen date,
                                 and the raw-stream artifact pointer go in.

EXPECTED first-run behaviour (read this before filing a bug about it)
----------------------------------------------------------------------
Dedup matches on the title prefix `Nightly go test -race — <module>`. The four
issues already open use the OLD title shape ("Nightly race detector flake — …"),
so they do not match, and the first scheduled run after this lands will file a
FIFTH issue. That is by design, not a regression of the bug fixed here.

Post-merge sequence the owner runs ONCE to collapse them:
  1. Retitle #1213 (the earliest/canonical one) to
     `Nightly go test -race — <module> — repeat-run failure in N test(s)`.
  2. Apply the `test-isolation` label to it.
  3. Close #1230 / #1258 / #1263 as duplicates of #1213.
After that the nightly finds #1213 by prefix and refreshes it in place forever.
Doing this BEFORE the first scheduled run avoids the fifth issue entirely.

Notification model (borrowed from scripts/ops/file_cve_report.sh)
------------------------------------------------------------------
The body is refreshed SILENTLY every night and the title carries the live
verdict — GitHub does not notify on a title or body edit. There is deliberately
NO nightly comment and NO auto-close:
  * a comment every morning on a known-failing test is muted within a week
    (that is literally how the 33-night CVE outage stayed invisible), and
  * one green night does not mean fixed — this is a REPEAT-RUN detector, so
    close/reopen churn would notify twice per flap and teach the reader nothing.
A green night is recorded as `last green: <date>` in the body instead.
Streak gating and delta-triggered comments are Tier 2 and deliberately out of
scope here; the state block below already carries `streak=` and `last_green=`
so Tier 2 needs no format change.

Wording is driven by EVIDENCE, not by this workflow's name
----------------------------------------------------------
The workflow is called "Nightly Race Detector" and its old body called every
failure a "race detector flake". The current failure is neither a race
(`grep -c "DATA RACE"` is 0 across all four nights) nor a flake (9/10, never
10/10). So the title says DATA RACE only when the parser actually saw the race
banner; otherwise it uses repeat-run / test-isolation vocabulary. This is the
follow-up the owner asked for in #932 triage and it was never done.

Fail-closed direction
---------------------
Every unknown resolves toward "worse", never toward "clean":
  * a MISSING or unparseable fragment is treated as a failing module (a leg that
    died before uploading has not proven anything green) AND exits non-zero,
  * an unreadable state block yields no baseline: first-seen is re-stamped but
    explicitly flagged in the body as possibly-earlier, never silently reset,
  * a label that cannot be created degrades to title-only dedup, still files the
    alert, and exits non-zero so the degradation cannot persist unnoticed.

Usage:
  file_race_report.py --frags-dir DIR --expected-modules a,b --run-url URL
Env:
  REPO                      owner/name (required when DO_FILE=true)
  DO_FILE  (default false)  "true" to actually create/edit issues
  DRY_RUN  (default 0)      "1" to echo gh argv instead of executing (offline tests)
  GITHUB_STEP_SUMMARY       appended to when set
Test seams (DRY_RUN=1 only; unset in CI, where they are inert):
  DRY_RUN_ISSUE_NUM         pretend this issue number already exists
  DRY_RUN_PREV_BODY         file standing in for that issue's current body
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

# The dedup key. Verified against the repo's existing labels: no collision, and
# `race-flake` (which the old header comment claimed) never existed at all.
LABEL = "test-isolation"
LABEL_COLOR = "D93F0B"
LABEL_DESC = "Nightly -race repeat-run / test-isolation failure (auto-filed, refreshed in place)"

# Machine-readable state carried inside the issue body. An HTML comment is
# invisible in rendered markdown but survives a body round-trip.
STATE_OPEN = "<!-- race-state:"
STATE_CLOSE = "-->"
STATE_RE = re.compile(re.escape(STATE_OPEN) + r"(.*?)" + re.escape(STATE_CLOSE), re.S)

TITLE_PREFIX_TMPL = "Nightly go test -race — {module}"


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
        ["gh", *args], capture_output=True, text=True, timeout=120, check=False
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
    """Create/refresh the dedup label. Returns False when it is UNUSABLE.

    Never swallowed with `|| true`: the label is the dedup key, and discarding
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
        "created. Falling back to title-based dedup so the alert still lands; fix "
        "the label so filtering works again."
    )
    return False


def find_issue(gh: Gh, repo: str, prefix: str, label_ok: bool) -> tuple[str, bool]:
    """Returns (issue number or "", lookup_ok).

    Deliberately NOT `--search`: the search index lags, and a lagging index would
    silently create a duplicate issue every night — the bug being fixed here.
    The label narrows the list; the per-module title PREFIX is what selects
    between the two modules sharing that one label. The unlabelled fallback runs
    whenever the labelled lookup came up empty, not only when the label is
    broken: a night that filed an UNLABELLED issue (label outage) would otherwise
    be invisible to the next night and get a duplicate.

    `lookup_ok=False` means the API call FAILED — which is NOT the same as "no
    matching issue exists", though the two used to collapse into the same empty
    string. A single 502 then fell through to the create branch and re-minted the
    duplicate this whole change removes, silently, at exit 0.

    On first run against the CURRENT repo state this legitimately returns
    ("", True): the four existing open issues are titled "Nightly race detector
    flake — …", which does not `startswith` the new prefix, so a fifth issue is
    filed by design. See the post-merge sequence in the module docstring.
    """
    if gh.dry_run:
        return os.environ.get("DRY_RUN_ISSUE_NUM", ""), True

    def _scan(args: list[str]) -> tuple[str, bool]:
        proc = gh.run(args)
        if proc.returncode != 0:
            gh.degradations.append(f"issue lookup for {prefix!r} (gh exited {proc.returncode})")
            print(
                f"::error title=Race report::issue lookup failed (gh exited "
                f"{proc.returncode}). Cannot tell 'no existing issue' from 'the "
                "API is down', so dedup is NOT trustworthy this run."
            )
            return "", False
        try:
            rows = json.loads(proc.stdout or "[]")
        except ValueError:
            gh.degradations.append(f"issue lookup for {prefix!r} (unparseable JSON)")
            print(
                "::error title=Race report::issue lookup returned unparseable JSON "
                "— treating dedup as unavailable rather than as 'nothing found'."
            )
            return "", False
        for row in rows:
            if str(row.get("title", "")).startswith(prefix):
                return str(row.get("number")), True
        return "", True

    base = ["issue", "list", "--repo", repo, "--state", "open",
            "--limit", "200", "--json", "number,title"]
    if label_ok:
        num, ok = _scan([*base, "--label", LABEL])
        if num or not ok:
            return num, ok
    # Unlabelled fallback. A failure HERE is what makes dedup untrustworthy,
    # because this is the query that would have found an unlabelled issue.
    return _scan(base)


def read_body(gh: Gh, repo: str, num: str) -> str:
    if gh.dry_run:
        path = os.environ.get("DRY_RUN_PREV_BODY")
        if path and Path(path).is_file():
            return Path(path).read_text(encoding="utf-8")
        return ""
    proc = gh.run(["issue", "view", num, "--repo", repo, "--json", "body", "--jq", ".body"])
    return proc.stdout if proc.returncode == 0 else ""


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
    for key in ("streak", "had_data_race", "build_failed", "unattributed",
                "tests_truncated"):
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
        "go_exit_code": -1, "fail_events": 0, "non_json_lines": 0, "total_events": 0,
        "raw_missing": True, "run_url": "", "workdir": "", "packages": "",
        "fragment_missing": True,
    }
    if not path.is_file():
        return stub, f"fragment for `{module}` is missing — its matrix leg never uploaded one"
    try:
        frag = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(frag, dict):
            raise ValueError("fragment is not an object")
    except (ValueError, OSError) as exc:
        return stub, f"fragment for `{module}` is unreadable ({exc})"
    frag.setdefault("module", module)
    frag["fragment_missing"] = False
    return frag, ""


# ── composition ──────────────────────────────────────────────────────────────


def verdict_suffix(frag: dict, green: bool, date: str) -> str:
    if green:
        return f"last run GREEN ({date})"
    if frag.get("fragment_missing"):
        return "NO RESULT — leg produced no fragment"
    if frag.get("build_failed"):
        return "build failure"
    if frag.get("had_data_race"):
        n = len(frag.get("race_tests") or []) or len(frag.get("failed_tests") or [])
        return f"DATA RACE ({n} test(s))" if n else "DATA RACE"
    if frag.get("failed_tests"):
        return f"repeat-run failure in {len(frag['failed_tests'])} test(s)"
    if frag.get("unattributed_failure"):
        return "failing, cause NOT attributed"
    return "failing"


def compose_body(frag: dict, state: dict, run_url: str, date: str,
                 baseline_readable: bool, degraded: str, green: bool,
                 lookup_ok: bool = True) -> str:
    module = frag["module"]
    tests = frag.get("failed_tests") or []
    warn = []
    if degraded:
        warn.append(f"🚨 **Degraded report** — {degraded}. Treat this module as UNVERIFIED, not clean.")
    if not lookup_ok:
        warn.append(
            "🚨 **Dedup was unavailable** — the existing-issue lookup failed, so this "
            "issue was filed WITHOUT being able to confirm no duplicate exists. "
            "Check for an older open issue for this module and close whichever is "
            "redundant. The alert is delivered loudly rather than skipped: a missed "
            "first night is worse than a duplicate you can see."
        )
    if not baseline_readable and state.get("_had_issue"):
        warn.append(
            "⚠️ The previous state block was unreadable, so **first-seen was re-stamped** "
            "— the real first occurrence may be earlier than shown below."
        )
    if frag.get("unattributed_failure") and not frag.get("fragment_missing"):
        warn.append(
            "⚠️ `go test` exited non-zero but no failing test, build failure, or race "
            "banner could be attributed. Read the raw stream artifact before assuming "
            "anything — this is the state the old workflow reported as if it were informative."
        )
    if frag.get("non_json_lines"):
        warn.append(
            f"ℹ️ {frag['non_json_lines']} non-JSON line(s) in the stream were skipped."
        )

    reproduce = (
        f"cd {frag.get('workdir') or '<workdir>'} && "
        f"go test {frag.get('packages') or './...'} -race -count=10"
    )
    first_run = state.get("first_run") or run_url
    rows = [
        "| | |", "|---|---|",
        f"| Verdict | {verdict_suffix(frag, green, date)} |",
        f"| Data race detected | {'**yes**' if frag.get('had_data_race') else 'no'} |",
        f"| Build failed | {'**yes**' if frag.get('build_failed') else 'no'} |",
        f"| First seen | {state.get('first_seen') or date} ([run]({first_run})) |",
        f"| Latest run | {date} ([run]({run_url})) |",
        f"| Last green | {state.get('last_green') or 'never recorded'} |",
        f"| Consecutive failing nights | {state.get('streak', 0)} |",
    ]

    out = [f"Nightly `go test -race -count=10` tracking issue for **{module}** on `main`.", ""]
    if warn:
        out += warn + [""]
    out += rows + [""]

    if tests:
        shown = tests[:MAX_RENDERED_TESTS]
        out += [
            f"## Failing tests ({len(tests)} distinct, deduped from "
            f"{frag.get('fail_events', 0)} fail events across 10 repetitions)",
            "",
        ] + [f"- `{t}`" for t in shown]
        if len(tests) > len(shown):
            out += [
                "",
                f"_… and {len(tests) - len(shown)} more (list capped at "
                f"{MAX_RENDERED_TESTS} to stay inside GitHub's {ISSUE_BODY_MAX}-char "
                f"body limit). A failure count this large usually means one shared "
                f"fixture broke the whole package, not {len(tests)} separate bugs — "
                f"the complete list is in the `race-raw-{module}` artifact._",
            ]
        out += [""]
    elif not green:
        out += ["## Failing tests", "", "_None could be attributed — see the warnings above._", ""]

    if frag.get("race_tests"):
        out += ["**Race banner attributed to:** "
                + ", ".join(f"`{t}`" for t in frag["race_tests"]), ""]

    if frag.get("had_data_race"):
        nature = ("A real data race was detected. This is a concurrency bug, not a flake — "
                  "the report below is reproducible evidence, not a sampling artifact.")
    elif frag.get("build_failed"):
        nature = "The package failed to BUILD under `-race`. No test result exists for this run."
    else:
        nature = (
            "**No data race was detected.** This is a repeat-run / test-isolation failure: "
            "the test fails when the suite runs 10x in one process, which usually means "
            "leaked state between repetitions (package-level vars, shared temp dirs, a "
            "singleton, an unclosed goroutine) rather than a concurrency bug. Do not "
            "describe it as a race in the fix."
        )
    out += ["## What this is", "", nature, ""]

    out += [
        "## Triage",
        "",
        f"1. Reproduce: `{reproduce}`",
        "2. Bisect the repetition count (`-count=2`, `-count=5`) to find where isolation breaks.",
        f"3. Full evidence — race report / panic / build error — is in the "
        f"`race-raw-{module}` artifact on [the run]({run_url}).",
        "",
        "This issue is **refreshed in place** every night (silent body edit — GitHub does "
        "not notify on title/body edits). It is deliberately NOT auto-closed: one green "
        "night does not prove a repeat-run failure is fixed. Close it by hand once the "
        "fix lands.",
        "",
        "Auto-filed by [`.github/workflows/nightly-race.yaml`]"
        "(https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/.github/workflows/nightly-race.yaml).",
        "",
    ]
    # The state block is appended AFTER clamping, never clamped itself: a
    # half-truncated `<!-- race-state: ... ` would be unparseable, and an
    # unparseable baseline makes the next night re-stamp first-seen and lose the
    # evidence of when this started. Prose is what gets cut; state survives.
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


def handle_module(gh: Gh, repo: str, module: str, frag: dict, degraded: str,
                  run_url: str, do_file: bool, label_ok: bool) -> str:
    """Files/refreshes this module's issue; returns its step-summary markdown."""
    date = today()
    prefix = TITLE_PREFIX_TMPL.format(module=module)
    green = not frag.get("failed")

    # On the FIRST scheduled run after this lands, `num` is legitimately empty:
    # the four existing open issues (#1213/#1230/#1258/#1263) are titled
    # "Nightly race detector flake — …" and do not match the new prefix, so a
    # fifth issue is filed BY DESIGN. The post-merge sequence that collapses
    # them is in the module docstring — a reader seeing issue #5 appear should
    # know it is planned, not a regression of the very bug this fixes.
    num, lookup_ok = find_issue(gh, repo, prefix, label_ok) if do_file else ("", True)
    prev_body = read_body(gh, repo, num) if num else ""
    prev = parse_state(prev_body)
    baseline_readable = prev is not None
    prev = prev or {}

    state = {
        "module": module,
        "first_seen": prev.get("first_seen") or ("" if green else date),
        # PRESERVE the first-seen run URL across refreshes: it is the only record
        # of when this started, and overwriting it nightly is how "four identical
        # issues" became indistinguishable from "one long-running failure".
        "first_run": prev.get("first_run") or ("" if green else run_url),
        "last_run": run_url,
        "last_green": date if green else prev.get("last_green", ""),
        "streak": 0 if green else int(prev.get("streak", 0)) + 1,
        "had_data_race": frag.get("had_data_race"),
        "build_failed": frag.get("build_failed"),
        "unattributed": frag.get("unattributed_failure"),
        "tests": frag.get("failed_tests") or prev.get("tests", []),
        "_had_issue": bool(num),
    }

    title = clamp_title(f"{prefix} — {verdict_suffix(frag, green, date)}")
    body = compose_body(frag, state, run_url, date, baseline_readable, degraded,
                        green, lookup_ok)

    summary = [f"### {module} — {verdict_suffix(frag, green, date)}"]
    if degraded:
        summary.append(f"🚨 {degraded}")
    if frag.get("failed_tests"):
        summary.append("Failing: " + ", ".join(f"`{t}`" for t in frag["failed_tests"]))

    if not do_file:
        summary.append("_(summary only — DO_FILE!=true, issues untouched)_")
        return "\n".join(summary)

    if green and not num and lookup_ok:
        summary.append("_(green, no open issue — nothing to do)_")
        return "\n".join(summary)
    if green and not lookup_ok:
        # Nothing to deliver on a green night, but the failed lookup still means
        # dedup is broken; main() reds the run off gh.failures.
        summary.append("_(green, but the issue lookup failed — dedup unverified)_")
        return "\n".join(summary)

    labels = ["bug", "ci", "P2"] + ([LABEL] if label_ok else [])
    if num:
        # EDIT, never comment: a nightly comment on a known-failing test is muted
        # within a week, and a muted alert is the same as no alert.
        print(f"refreshing #{num} for {module} (silent body edit)")
        ok = gh.must(
            ["issue", "edit", num, "--repo", repo, "--title", title, "--body", body],
            f"refreshing issue #{num} for {module}",
        )
        summary.append(
            f"Refreshed issue #{num} (silent)." if ok
            else f"❌ FAILED to refresh issue #{num} — the alert did NOT land."
        )
    else:
        # Dedup could not be confirmed -> file anyway. A duplicate is visible and
        # closable; a skipped first night is silence, which is the failure mode
        # this workflow exists to remove. The run still goes red.
        print(f"filing new tracking issue for {module}"
              + ("" if lookup_ok else " (WITHOUT a trustworthy dedup lookup)"))
        args = ["issue", "create", "--repo", repo, "--title", title,
                "--assignee", "vencil", "--body", body]
        for lab in labels:
            args += ["--label", lab]
        ok = gh.must(args, f"filing the tracking issue for {module}")
        summary.append(
            "Filed a new tracking issue." if ok
            else "❌ FAILED to file the tracking issue — the alert did NOT land."
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
        with open(summary_path, "a", encoding="utf-8") as fh:
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
            "The tracking issue does NOT reflect this run."
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
            "dedup is degraded and WILL duplicate issues until the label is fixed."
        )
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
