#!/usr/bin/env python3
"""diag_pr_ci.py — PR CI auto-diagnostic CLI (issue #446).

Wraps 4 GitHub REST endpoints via `gh api` to summarize a PR's failing CI
checks, drilling from check-runs → workflow jobs → annotations into a single
markdown (or JSON) report.

Why `gh api` not `requests` (#446 day-2 review architecture pivot)
-----------------------------------------------------------------
- Auth inherits `gh auth login` — contributors don't manage PAT env vars.
- `--paginate` handles Matrix builds with >30 check-runs transparently.
- gh handles rate-limit / retry / human-friendly error messages.
- No new "github.com from Python" precedent to drift from — `scripts/`
  has 10 `requests`-using scripts but none target github.com.

Three prerequisite exit codes (so CI / readers can disambiguate)
-----------------------------------------------------------------
- exit 2: `gh` missing or unauthenticated. Operator action: install /
  `gh auth login`.
- exit 3: api.github.com unreachable from this host (Cowork VM proxy is
  the common cause). Operator action: switch to Windows MCP / Dev Container.
- exit 0: tool succeeded (regardless of whether CI failures were found).
- exit 1: tool internal error (subprocess / JSON parse / bug).

The diagnostic itself succeeding when CI is red is intentional — this is a
read-only inspector, not a gate.

Output formats
--------------
- `--markdown` (default, human-readable): per-failed-check section with up
  to `--max-annotations-per-check` annotations (default 5). Truncates the
  rest with a `... N more` line and points at the GitHub UI / `--json`.
  Keeps output well under the 65K char GitHub PR-comment limit (which can
  bite when a mypy-spewing PR is diagnosed).
- `--json`: unbounded raw structure for machine consumers. Stable schema.

Usage
-----
    python scripts/tools/dx/diag_pr_ci.py 446
    python scripts/tools/dx/diag_pr_ci.py 446 --json > diag.json
    make diag-pr ARGS="446"
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

# Windows cp950 / cp936 consoles can't encode ✅⚠️❌ — fail-safe to UTF-8.
if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# Exit codes — documented in module docstring, kept as module constants so
# call sites and tests don't drift from the spec.
EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_PREREQ_MISSING = 2
EXIT_NETWORK_BLOCKED = 3


# ─── Errors ─────────────────────────────────────────────────────────────


class GhApiError(RuntimeError):
    """Raised when `gh api` fails after the prerequisite probe passed.

    Carries the gh-emitted stderr so callers can surface it verbatim. We
    don't normalise the message because gh's own diagnostics are already
    human-friendly — re-wrapping would erase information.
    """

    def __init__(self, endpoint: str, stderr: str, returncode: int) -> None:
        super().__init__(f"gh api {endpoint} failed (rc={returncode}): {stderr}")
        self.endpoint = endpoint
        self.stderr = stderr
        self.returncode = returncode


# ─── gh api wrapper ─────────────────────────────────────────────────────


def gh_api(path: str, paginate: bool = False, timeout: int = 30) -> Any:
    """Call `gh api <path>` and parse JSON stdout.

    Args:
      path: API path, e.g. `/repos/owner/repo/pulls/446`.
      paginate: pass `--paginate` so gh follows Link headers and joins pages.
        When the endpoint returns an object containing a paginated array
        (e.g. `/check-runs` whose array is under `.check_runs`), gh joins
        the arrays internally — callers don't need to merge pages manually.
      timeout: per-call timeout in seconds. gh's own retry/backoff sits
        under this.

    Returns parsed JSON (dict | list).

    Raises:
      GhApiError: gh exited non-zero.
      ValueError: stdout was not parseable JSON.
    """
    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    cmd.append(path)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        # Network can drop mid-flow even after the 3s prereq probe passed.
        # Surface as a normal GhApiError so main()'s except clause handles
        # it cleanly instead of leaking a stack trace to stderr.
        raise GhApiError(
            path,
            f"gh api timed out after {timeout}s (network may have dropped)",
            124,  # POSIX convention for command-line tool timeout
        )
    if proc.returncode != 0:
        raise GhApiError(path, (proc.stderr or "").strip(), proc.returncode)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"gh api {path} returned non-JSON: {proc.stdout[:200]}") from e


# ─── Prerequisite probe ────────────────────────────────────────────────


def check_prerequisites() -> None:
    """Three-step prerequisite probe with distinct exit codes.

    1. `gh --version` — is gh installed at all?
    2. `gh auth status` — is the user authenticated?
    3. `gh api /rate_limit` with a 3s timeout — can we reach api.github.com?

    On any failure prints an actionable hint to stderr and `sys.exit()`s
    with a distinct code so callers (CI, humans) can disambiguate without
    parsing stderr.

    Exits 2 for steps 1 & 2 (operator can fix locally), 3 for step 3
    (operator must change host/network).
    """
    # Step 1: gh on PATH at all.
    try:
        subprocess.run(
            ["gh", "--version"],
            capture_output=True, check=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(
            "❌ `gh` CLI not found or not runnable on this host.\n"
            "   Install: https://cli.github.com/",
            file=sys.stderr,
        )
        sys.exit(EXIT_PREREQ_MISSING)
    except subprocess.TimeoutExpired:
        # gh --version shouldn't take >3s; treat as broken install.
        print(
            "❌ `gh --version` timed out — gh install appears broken.\n"
            "   Reinstall: https://cli.github.com/",
            file=sys.stderr,
        )
        sys.exit(EXIT_PREREQ_MISSING)

    # Step 2: authenticated. `gh auth status` exits non-zero if not logged in.
    auth = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=5, check=False,
    )
    if auth.returncode != 0:
        print(
            "❌ `gh` is not authenticated.\n"
            "   Run: gh auth login",
            file=sys.stderr,
        )
        sys.exit(EXIT_PREREQ_MISSING)

    # Step 3: can we actually reach api.github.com? 3s probe — distinguish
    # "proxy blocks api.github.com" (Cowork VM symptom) from "host has no
    # network at all". Either way, the right user action is to switch
    # execution environment.
    try:
        probe = subprocess.run(
            ["gh", "api", "/rate_limit"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=3, check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            "⚠️  Timed out reaching api.github.com (3s probe).\n"
            "   Common cause: Cowork VM proxy blocks api.github.com.\n"
            "   Switch to Windows MCP or Dev Container and retry.",
            file=sys.stderr,
        )
        sys.exit(EXIT_NETWORK_BLOCKED)

    if probe.returncode != 0:
        stderr_lower = (probe.stderr or "").lower()
        proxy_signal = any(s in stderr_lower for s in ("proxy", "timeout", "connection refused", "could not resolve"))
        if proxy_signal:
            print(
                "⚠️  api.github.com unreachable (looks like a proxy / network block).\n"
                "   Switch to Windows MCP or Dev Container and retry.\n"
                f"   gh stderr: {probe.stderr.strip()}",
                file=sys.stderr,
            )
            sys.exit(EXIT_NETWORK_BLOCKED)
        # Auth-class error (token expired, etc.) — re-suggest re-login.
        print(
            f"❌ `gh api /rate_limit` probe failed: {probe.stderr.strip()}\n"
            "   Run `gh auth status` and `gh auth refresh` to diagnose.",
            file=sys.stderr,
        )
        sys.exit(EXIT_PREREQ_MISSING)


# ─── Repo detection ────────────────────────────────────────────────────


def detect_repo() -> str:
    """Resolve `owner/repo` from `gh repo view`.

    The diagnostic operates against whatever repo gh is currently pointed
    at. Cross-repo invocation is out of scope for #446 (one tool, one
    common diagnostic flow) — if a user needs to inspect a different repo
    they can `cd` into that clone first.
    """
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=5, check=False,
    )
    if proc.returncode != 0:
        raise GhApiError("repo view", (proc.stderr or "").strip(), proc.returncode)
    data = json.loads(proc.stdout)
    return data["nameWithOwner"]


# ─── Data model ────────────────────────────────────────────────────────


# Match the workflow-run-id segment in a check-run's details_url:
#   https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
# Some check-runs (external apps like CodeCov, Trivy) have a different URL
# shape with no actions-run-id — we treat those as "no jobs available".
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")


@dataclass
class JobStep:
    name: str
    status: str  # queued / in_progress / completed
    conclusion: str | None  # success / failure / cancelled / skipped / null


@dataclass
class JobDiag:
    job_id: int
    name: str
    status: str
    conclusion: str | None
    failed_steps: list[JobStep] = field(default_factory=list)
    html_url: str = ""


@dataclass
class CheckDiag:
    check_run_id: int
    name: str
    conclusion: str | None  # success / failure / cancelled / skipped / neutral / timed_out / action_required / null
    details_url: str
    summary: str = ""  # check-run output summary (often empty)
    workflow_run_id: int | None = None
    jobs: list[JobDiag] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PrDiag:
    pr_number: int
    pr_title: str
    pr_url: str
    head_sha: str
    head_ref: str
    state: str  # open / closed / merged
    failed_checks: list[CheckDiag] = field(default_factory=list)
    other_checks_summary: dict[str, int] = field(default_factory=dict)


# ─── Endpoint callers ──────────────────────────────────────────────────


def get_pull(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    """Fetch PR head info (sha, ref, state, title, url)."""
    return gh_api(f"/repos/{owner}/{repo}/pulls/{pr_number}")


def get_check_runs(owner: str, repo: str, head_sha: str) -> list[dict[str, Any]]:
    """Fetch all check-runs for a commit. `--paginate` covers Matrix builds.

    The endpoint returns `{"total_count": N, "check_runs": [...]}`. With
    `--paginate`, gh joins pages into a SINGLE wrapper dict (not list);
    we extract `.check_runs` from the merged result.
    """
    result = gh_api(
        f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
        paginate=True,
    )
    return result.get("check_runs", []) if isinstance(result, dict) else []


def get_workflow_jobs(owner: str, repo: str, run_id: int) -> list[dict[str, Any]]:
    """Fetch job-level breakdown for an Actions workflow run."""
    result = gh_api(
        f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
        paginate=True,
    )
    return result.get("jobs", []) if isinstance(result, dict) else []


def get_check_run_annotations(
    owner: str, repo: str, check_run_id: int
) -> list[dict[str, Any]]:
    """Fetch annotations attached to a check-run (file/line/message tuples).

    Note: file/line annotations are produced by reusable-workflow-style CI
    (mypy, ruff, pytest annotation plugins, etc.). External app checks
    often have zero annotations even when they "failed" — the failure
    reason lives in the check-run summary instead.
    """
    result = gh_api(
        f"/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations",
        paginate=True,
    )
    return result if isinstance(result, list) else []


# ─── Orchestration ─────────────────────────────────────────────────────


def diagnose_pr(owner: str, repo: str, pr_number: int) -> PrDiag:
    """Drive the 4-endpoint sequence and assemble a PrDiag.

    Endpoint order is meaningful (subsequent calls depend on prior data):
      1. `/pulls/{n}`           → head_sha (input to 2)
      2. `/commits/{sha}/check-runs` → per-check details_url, conclusion
      3. `/actions/runs/{id}/jobs` (per failed Actions check-run only) →
                                    job-level breakdown
      4. `/check-runs/{id}/annotations` (per failed check-run) →
                                    file/line messages

    For external-app check-runs (no `/actions/runs/<id>/` in details_url —
    e.g. CodeCov, Trivy SaaS), step 3 is skipped silently. Annotations
    still attempt because external apps sometimes emit them too.

    Returns:
      PrDiag with `failed_checks` populated (drilled) and
      `other_checks_summary` counting passed/pending/skipped buckets.
    """
    pr = get_pull(owner, repo, pr_number)
    diag = PrDiag(
        pr_number=pr_number,
        pr_title=pr.get("title", ""),
        pr_url=pr.get("html_url", ""),
        head_sha=pr["head"]["sha"],
        head_ref=pr["head"]["ref"],
        state="merged" if pr.get("merged") else pr.get("state", "unknown"),
    )

    check_runs = get_check_runs(owner, repo, diag.head_sha)
    bucket_counts: dict[str, int] = {}
    for cr in check_runs:
        conclusion = cr.get("conclusion")
        if conclusion in ("failure", "timed_out", "action_required", "cancelled"):
            diag.failed_checks.append(_drill_failed_check(owner, repo, cr))
        else:
            # Group everything else by conclusion (or "pending" if null).
            key = conclusion if conclusion else "pending"
            bucket_counts[key] = bucket_counts.get(key, 0) + 1
    diag.other_checks_summary = bucket_counts

    return diag


def _drill_failed_check(
    owner: str, repo: str, cr: dict[str, Any]
) -> CheckDiag:
    """Drill a failed check-run: fetch jobs (if Actions) + annotations."""
    check = CheckDiag(
        check_run_id=cr["id"],
        name=cr.get("name", "<unnamed>"),
        conclusion=cr.get("conclusion"),
        details_url=cr.get("details_url", ""),
        summary=(cr.get("output", {}) or {}).get("summary", "") or "",
    )

    match = _RUN_ID_RE.search(check.details_url)
    if match:
        check.workflow_run_id = int(match.group(1))
        try:
            jobs = get_workflow_jobs(owner, repo, check.workflow_run_id)
        except GhApiError as e:
            # Jobs-level failure shouldn't abort the whole diag — log and
            # continue. Empty jobs list will surface as "no job breakdown
            # available" in markdown output, which is more useful than a
            # 1-failed-check error from the tool itself.
            print(
                f"⚠️  Could not fetch jobs for run {check.workflow_run_id}: {e.stderr}",
                file=sys.stderr,
            )
            jobs = []
        for j in jobs:
            if j.get("conclusion") not in ("failure", "timed_out", "cancelled"):
                continue
            failed_steps = [
                JobStep(
                    name=s.get("name", ""),
                    status=s.get("status", ""),
                    conclusion=s.get("conclusion"),
                )
                for s in (j.get("steps") or [])
                if s.get("conclusion") in ("failure", "timed_out", "cancelled")
            ]
            check.jobs.append(JobDiag(
                job_id=j["id"],
                name=j.get("name", ""),
                status=j.get("status", ""),
                conclusion=j.get("conclusion"),
                failed_steps=failed_steps,
                html_url=j.get("html_url", ""),
            ))

    # Annotations attempt regardless of Actions/external — some external
    # checks (e.g. linters running as GitHub apps) attach them.
    try:
        check.annotations = get_check_run_annotations(owner, repo, check.check_run_id)
    except GhApiError as e:
        print(
            f"⚠️  Could not fetch annotations for check {check.check_run_id}: {e.stderr}",
            file=sys.stderr,
        )

    return check


# ─── Formatters ────────────────────────────────────────────────────────


def format_json(diag: PrDiag) -> str:
    """Stable JSON for machine consumers — no truncation."""
    return json.dumps(asdict(diag), indent=2, ensure_ascii=False)


def format_markdown(diag: PrDiag, max_annotations_per_check: int = 5) -> str:
    """Human-readable markdown. Per-check annotation list capped to
    `max_annotations_per_check` entries to keep output under the 65K char
    limit that GitHub PR comments / CI surfaces sometimes enforce. The
    rest are summarised as `... N more — see GitHub UI or --json`.
    """
    lines: list[str] = []
    lines.append(f"# PR #{diag.pr_number} — {diag.pr_title}")
    lines.append("")
    lines.append(f"- **State**: {diag.state}")
    lines.append(f"- **Head**: `{diag.head_sha[:12]}` on `{diag.head_ref}`")
    lines.append(f"- **URL**: {diag.pr_url}")

    if diag.other_checks_summary:
        summary_parts = ", ".join(
            f"{k}={v}" for k, v in sorted(diag.other_checks_summary.items())
        )
        lines.append(f"- **Other checks**: {summary_parts}")
    lines.append("")

    if not diag.failed_checks:
        lines.append("✅ No failed checks.")
        return "\n".join(lines)

    lines.append(f"## ❌ {len(diag.failed_checks)} failed check(s)")
    lines.append("")

    for check in diag.failed_checks:
        lines.append(f"### {check.name} — `{check.conclusion}`")
        if check.details_url:
            lines.append(f"- Logs: {check.details_url}")
        if check.summary.strip():
            lines.append(f"- Summary: {check.summary.strip()[:200]}")

        if check.jobs:
            lines.append("- Failed jobs:")
            for job in check.jobs:
                lines.append(f"  - **{job.name}** ({job.conclusion}) — {job.html_url}")
                for step in job.failed_steps[:3]:
                    lines.append(f"    - step: `{step.name}` ({step.conclusion})")
                if len(job.failed_steps) > 3:
                    lines.append(f"    - … and {len(job.failed_steps) - 3} more failed step(s)")
        elif check.workflow_run_id:
            # Empty job list can mean (a) the /jobs call errored (stderr has
            # the warning), (b) the run was cancelled before any job started,
            # or (c) no individual job had conclusion=failure even though the
            # check rolled up to failure. Neutral wording avoids
            # over-claiming "gh api error".
            lines.append(
                f"- _No failed jobs surfaced via `/actions/runs/{check.workflow_run_id}/jobs`. "
                "Check the run page or rerun with `--json` for the raw response._"
            )
        else:
            lines.append("- _External-app check — no Actions jobs to drill_")

        if check.annotations:
            shown = check.annotations[:max_annotations_per_check]
            lines.append(f"- Annotations ({len(check.annotations)}):")
            for ann in shown:
                path = ann.get("path", "<no-path>")
                line = ann.get("start_line", "?")
                # Defensive: strip + take first line only if there IS a line
                # after stripping. Whitespace-only messages collapse to "".
                raw_msg = (ann.get("message") or "").strip()
                first_line = raw_msg.splitlines()[0] if raw_msg else ""
                level = ann.get("annotation_level", "")
                lines.append(f"  - `{path}:{line}` [{level}] {first_line[:120]}")
            if len(check.annotations) > max_annotations_per_check:
                hidden = len(check.annotations) - max_annotations_per_check
                lines.append(
                    f"  - … and {hidden} more — see GitHub UI or rerun with `--json`."
                )
        lines.append("")

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize failing CI checks for a PR via gh api (#446).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  diagnostic succeeded (whether or not checks failed)
  1  internal error (subprocess / parse / bug)
  2  `gh` missing or not authenticated — install / `gh auth login`
  3  api.github.com unreachable — switch to Windows MCP / Dev Container

Example:
  %(prog)s 446
  %(prog)s 446 --json > diag.json
""",
    )
    parser.add_argument("pr_number", type=int, help="PR number to diagnose")
    fmt_group = parser.add_mutually_exclusive_group()
    fmt_group.add_argument(
        "--markdown", action="store_const", dest="format", const="markdown",
        help="Human-readable markdown output (default)",
    )
    fmt_group.add_argument(
        "--json", action="store_const", dest="format", const="json",
        help="Machine-readable JSON output (no truncation)",
    )
    parser.add_argument(
        "--max-annotations-per-check",
        type=int, default=5,
        help="Annotation lines per failed check in markdown mode (default: 5; "
             "keeps output under the 65K GitHub PR-comment limit)",
    )
    parser.set_defaults(format="markdown")
    args = parser.parse_args()

    check_prerequisites()

    try:
        repo_full = detect_repo()
    except GhApiError as e:
        print(f"❌ Could not detect repo: {e.stderr}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    owner, _, repo = repo_full.partition("/")

    try:
        diag = diagnose_pr(owner, repo, args.pr_number)
    except GhApiError as e:
        # Distinguish PR-not-found (404) from generic failure so the user
        # gets a pointed error instead of a wall of gh stderr.
        if "404" in e.stderr or "not found" in e.stderr.lower():
            print(
                f"❌ PR #{args.pr_number} not found in {repo_full}.\n"
                f"   Verify the PR number; cross-repo diagnosis is out of scope.",
                file=sys.stderr,
            )
            return EXIT_INTERNAL_ERROR
        print(f"❌ gh api failed: {e}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    except (KeyError, ValueError) as e:
        print(f"❌ Unexpected gh api response shape: {e}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    if args.format == "json":
        print(format_json(diag))
    else:
        print(format_markdown(
            diag, max_annotations_per_check=args.max_annotations_per_check,
        ))

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
