#!/usr/bin/env python3
"""analyze_tier1_fp_rate.py — Tier 1 bench-gate friction-rate observer (issue #433 W3).

Purpose
-------
Issue #433 W3 closure requires observing how often the Tier 1 bench-gate
requires maintainer intervention (override label applied, or PR merged despite
a red gate). This tool queries recent `bench-gate-pr.yaml` workflow runs and
the corresponding PR states, categorizes each merged PR, and computes a
"friction rate".

⚠️ IMPORTANT — what this metric IS and ISN'T (post-Gemini-W3-review)
-------------------------------------------------------------------
The metric reported is **friction rate**, NOT a strict false-positive rate.
This tool inspects only the TERMINAL state of each PR (latest run + current
labels), so the categorization is subject to a fundamental **survivorship
bias**:

    Scenario: a real perf regression PR. Tier 1 catches it (red gate).
              Dev fixes the code. Subsequent run is green. PR merges.

    This is a TRUE POSITIVE for the gate — exactly why the gate exists.
    But to this tool, the PR's terminal state is "workflow success + no
    override + merged" → bucketed as `passed-clean`. The TP catch is
    INVISIBLE in the report.

Consequence: this tool **measures friction (interventions)** but **cannot
quantify the gate's defensive value (caught bugs)**. The friction rate is
an UPPER BOUND on the unwanted-friction count, not a "true FP rate".

For W3's actual question ("is the gate annoying enough to escalate to Larger
Runners?"), friction rate IS the right metric — escalation cost is justified
when intervention frequency is high regardless of whether each was a true FP
or a deliberate trade-off. Just be honest about what the number represents.

Decision matrix (per #433 W3 spec, post-v5 reality):

    Friction rate < 10%  → ✅ no escalation; W3 closeable
    Friction rate 10-25% → ⏳ review root cause (within-runner variance vs flake)
    Friction rate > 25%  → 🚨 escalation PR: switch runs-on to ubuntu-latest-4-cores

Per the simplified W3 framing (judgment call, not threshold-driven), these
ranges are guides, not hard cutoffs. Eyeball the bucket distribution; if
0-1 overrides out of 5-10 PRs → fine. If ≥30% interventions → broken.

Categorization (revised after self-review; see "Bug history" below)
------------------------------------------------------------------
For each merged PR, look at the LATEST Tier 1 run + current PR label state:

    passed-clean        workflow success + no override label
                          = gate happy, no maintainer intervention needed.
                          Counts as TRUE NEGATIVE.

    override-applied    override label currently on PR (and PR merged)
                          = maintainer applied override, the merging run
                          honored it (skip=true → workflow conclusion was
                          actually "success" even though no bench ran).
                          Counts as FALSE POSITIVE signal — maintainer
                          believed the gate flag was either a deliberate
                          trade-off OR not-actually-a-regression.

    merged-despite-red  workflow failure + PR merged + no override label
                          = maintainer manually merged with red gate
                          (only possible without branch protection on
                          this check). Counts as FALSE POSITIVE-LIKE
                          signal — the gate was ignored.

    open                PR still open; no terminal signal yet (excluded).
    unmerged            PR closed without merge (excluded).

friction rate = (override-applied + merged-despite-red)
              / (override-applied + merged-despite-red + passed-clean)

(NOT counted in denominator: open / unmerged / merged-other-state)

Bug history (v1 → v2, this revision)
------------------------------------
v1 of this tool had a categorization bug: it bucketed override-skipped
runs as `passed` because workflow conclusion is "success" when preflight
short-circuits via skip=true (bench/compare jobs cascade-skip via
`needs:` + `if:`). That UNDER-COUNTED FPs significantly — any PR with
override label applied would appear "passing cleanly". v2 (this file)
categorizes by maintainer-action-signal (label state + merge outcome)
rather than workflow_conclusion alone.

Limitations / honest caveats
----------------------------
1. We can't distinguish "override accepted real regression as a deliberate
   trade-off" from "override applied because gate was wrong (true FP)".
   Both count toward the FP-rate metric. For W3's "is the gate annoying
   enough to warrant Larger Runners" question, the conflation is acceptable
   — both represent "maintainer felt the gate verdict was not actionable".

2. We don't distinguish workflow-flake (network, timeout, etc.) from
   true regression in the `merged-despite-red` bucket. If the workflow
   itself flakes often, this bucket inflates. Look at workflow conclusion
   distribution (`gh run list --workflow bench-gate-pr.yaml`) separately
   for flake signal.

3. Open / unmerged PRs are excluded. If many PRs are stuck open with red
   bench-gate, that's a separate signal worth noting manually (the gate
   is acting as a soft blocker on developer pace).

Usage
-----
::

    # Default: latest 30 Tier 1 runs
    python3 scripts/tools/dx/analyze_tier1_fp_rate.py

    # Custom window
    python3 scripts/tools/dx/analyze_tier1_fp_rate.py --limit 50

    # JSON output for scripting
    python3 scripts/tools/dx/analyze_tier1_fp_rate.py --json

    # Verbose: show per-run details
    python3 scripts/tools/dx/analyze_tier1_fp_rate.py --verbose

Exit codes
----------
    0  Always (this is observation tooling; doesn't gate anything).
       The W3 decision based on output is a manual maintainer call.

Stdlib-only by design. Uses `gh` CLI for GitHub API access (must be
authenticated). Cross-platform compatible via `try_utf8_stdout()` shared
helper (cp950 / cp936 / cp1252 Windows-console resilience).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# stdlib-only sys.path setup — see _lib_compat docstring for rationale.
_LIB_DIR = Path(__file__).resolve().parent.parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from _lib_compat import try_utf8_stdout  # noqa: E402

REPO = "vencil/Dynamic-Alerting-Integrations"
WORKFLOW = "bench-gate-pr.yaml"
OVERRIDE_LABEL = "override: bench-regress-ok"

# Friction-rate thresholds for W3 decision (per issue #433).
# Naming note: "friction rate" not "FP rate" because the metric measures
# maintainer-intervention frequency, not strict false-positives. See module
# docstring "What this metric IS and ISN'T" for the survivorship-bias caveat.
FRICTION_RATE_OK = 10.0      # below = no escalation
FRICTION_RATE_REVIEW = 25.0  # 10-25 = review; above 25 = escalate


@dataclass
class RunRecord:
    """One bench-gate-pr.yaml run + its PR's terminal state."""
    run_id: int
    pr_number: Optional[int]
    pr_url: Optional[str]
    workflow_conclusion: str  # success / failure / cancelled / etc.
    pr_state: str             # OPEN / MERGED / CLOSED
    pr_labels: list[str] = field(default_factory=list)
    bucket: str = ""          # set by categorize()
    note: str = ""

    def categorize(self) -> None:
        # Open PR: no terminal signal yet.
        if self.pr_state == "OPEN":
            self.bucket = "open"
            return

        # PR closed without merge: exclude — could be abandoned for many
        # reasons unrelated to bench-gate.
        if self.pr_state == "CLOSED":
            self.bucket = "unmerged"
            return

        # PR merged. Categorize by maintainer-action-signal:
        # - Override label present? → maintainer overrode the gate
        # - Workflow success + no override? → gate happy, no intervention
        # - Workflow failure + no override + merged? → maintainer ignored
        #   red gate and merged anyway (only possible w/o branch protection)
        #
        # Check override BEFORE workflow_conclusion because the override
        # path produces workflow_conclusion="success" (preflight short-
        # circuits → bench/compare cascade-skip → workflow overall green).
        # v1 of this tool checked conclusion first and missed override-
        # skipped runs entirely — bucketed them as `passed`. See module
        # docstring's "Bug history" for context.
        has_override = OVERRIDE_LABEL in self.pr_labels

        if has_override:
            self.bucket = "override-applied"
            return

        if self.workflow_conclusion == "success":
            self.bucket = "passed-clean"
            return

        # Workflow failed-class. Be explicit about `failure` vs other states
        # (`cancelled`, `timed_out`, `skipped`, `action_required`, `neutral`).
        # `merged-despite-red` should ONLY count actual workflow `failure`
        # — a cancelled run (concurrency cancel-in-progress) merged-after
        # is NOT maintainer ignoring a red gate. [Gemini round-W3 catch.]
        if self.workflow_conclusion == "failure":
            self.bucket = "merged-despite-red"
            return

        # Non-success, non-failure (cancelled / timed_out / skipped / etc.)
        # → ambiguous. Don't inflate the friction count with these.
        self.bucket = "merged-other-state"


def _gh(cmd: list[str], timeout: int = 60) -> str:
    """Run a `gh` command; return stdout. Raises CalledProcessError on failure."""
    proc = subprocess.run(
        ["gh", *cmd],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["gh", *cmd], proc.stdout, proc.stderr
        )
    return proc.stdout


def list_tier1_runs(limit: int) -> list[dict]:
    """Get recent Tier 1 workflow runs (any conclusion — we categorize later)."""
    try:
        out = _gh([
            "run", "list",
            "--workflow", WORKFLOW,
            "--repo", REPO,
            "--limit", str(limit),
            "--json", "databaseId,conclusion,event,headBranch,headSha,createdAt",
            "--event", "pull_request",
        ])
        return json.loads(out)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: gh run list failed — {e.stderr.strip()}", file=sys.stderr)
        if "authentication" in e.stderr.lower() or "auth" in e.stderr.lower():
            print("       Run `gh auth login` first.", file=sys.stderr)
        sys.exit(2)


def find_pr_for_run(head_sha: str) -> Optional[dict]:
    """Resolve a workflow run's head SHA to its PR via GitHub commit-pulls API.

    `gh run view --json pullRequests` is NOT a valid field (despite intuition);
    the correct path is /repos/{owner}/{repo}/commits/{sha}/pulls which lists
    PRs containing a given commit. Returns the first match (usually one PR per
    commit; multi-PR cherry-picks are rare in this repo).
    """
    try:
        out = _gh([
            "api", f"repos/{REPO}/commits/{head_sha}/pulls",
            "--jq", "[.[] | {number, state, url}]",
        ])
        prs = json.loads(out)
        return prs[0] if prs else None
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return None


def get_pr_terminal_state(pr_number: int) -> tuple[str, list[str]]:
    """Get PR's terminal state + labels. Returns (state, [label_names])."""
    try:
        out = _gh([
            "pr", "view", str(pr_number),
            "--repo", REPO,
            "--json", "state,labels",
        ])
        data = json.loads(out)
        labels = [lbl["name"] for lbl in data.get("labels", [])]
        return data.get("state", "UNKNOWN"), labels
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return "UNKNOWN", []


def collect_records(limit: int, verbose: bool = False) -> list[RunRecord]:
    """Pull recent Tier 1 runs and resolve each to a categorized RunRecord."""
    runs = list_tier1_runs(limit)
    records: list[RunRecord] = []
    seen_prs: set[int] = set()  # dedupe — multiple runs per PR (rerun, sync)

    for i, run in enumerate(runs, 1):
        if verbose:
            print(f"  [{i}/{len(runs)}] resolving run {run['databaseId']} "
                  f"(sha={run['headSha'][:8]})...",
                  file=sys.stderr)
        pr = find_pr_for_run(run["headSha"])
        if not pr:
            if verbose:
                print(f"    no PR found for sha {run['headSha'][:8]}", file=sys.stderr)
            continue
        pr_number = pr.get("number")
        if not pr_number or pr_number in seen_prs:
            # Skip duplicate runs for the same PR; only count one signal per PR.
            continue
        seen_prs.add(pr_number)

        state, labels = get_pr_terminal_state(pr_number)
        # Construct human-readable PR URL (the commits/pulls API returns the
        # API URL, not the github.com/.../pull/N URL we want for display).
        human_url = f"https://github.com/{REPO}/pull/{pr_number}"
        rec = RunRecord(
            run_id=run["databaseId"],
            pr_number=pr_number,
            pr_url=human_url,
            workflow_conclusion=run.get("conclusion", "unknown"),
            pr_state=state,
            pr_labels=labels,
        )
        rec.categorize()
        records.append(rec)
    return records


def compute_stats(records: list[RunRecord]) -> dict:
    """Aggregate categorized records into friction rate + bucket counts."""
    buckets = {
        "passed-clean": 0,
        "override-applied": 0,
        "merged-despite-red": 0,
        "merged-other-state": 0,  # cancelled / timed_out / skipped / etc.
        "open": 0,
        "unmerged": 0,
    }
    for r in records:
        buckets[r.bucket] = buckets.get(r.bucket, 0) + 1

    # Friction numerator: maintainer interventions.
    friction_count = buckets["override-applied"] + buckets["merged-despite-red"]
    # Denominator: merged PRs with clear terminal gate signal. EXCLUDE
    # `merged-other-state` (cancelled/timed_out/skipped — ambiguous as to
    # whether the gate had any opinion) so it doesn't inflate the rate.
    total_with_signal = buckets["passed-clean"] + friction_count
    friction_rate = (friction_count / total_with_signal * 100) if total_with_signal else 0.0

    return {
        "total_unique_prs": len(records),
        "buckets": buckets,
        "friction_count": friction_count,
        "friction_denominator": total_with_signal,
        "friction_rate_pct": round(friction_rate, 2),
        "total_with_signal": total_with_signal,
        "stopping_condition": _check_stopping(buckets, friction_rate, total_with_signal),
        "recommendation": _recommend(friction_rate, total_with_signal),
    }


def _check_stopping(buckets: dict, friction_rate: float, total: int) -> str:
    """Which W3 stopping condition is hit (if any)."""
    # Clear-signal early close: ≥5-10 runs AND result clearly above/below 10%
    # (matches the simplified W3 framing — judgment call, not hard threshold).
    if total >= 5 and (friction_rate < 5.0 or friction_rate > 20.0):
        return "clear signal — close-able by judgment"
    if total >= 30:
        return "ample data — close-able by judgment"
    if total < 5:
        return f"insufficient data ({total}/5 minimum for meaningful signal)"
    return f"continue accumulating ({total} runs so far, borderline friction={friction_rate:.1f}%)"


def _recommend(friction_rate: float, total: int) -> str:
    """W3 decision recommendation (judgment guidance, not a hard rule)."""
    if total < 5:
        return "🟡 INSUFFICIENT_DATA — wait for more Tier 1 runs (~5 minimum)"
    if friction_rate < FRICTION_RATE_OK:
        return f"✅ LOW_FRICTION — friction rate {friction_rate:.1f}% < {FRICTION_RATE_OK}%; no escalation likely needed"
    if friction_rate < FRICTION_RATE_REVIEW:
        return (f"⏳ MODERATE_FRICTION — friction rate {friction_rate:.1f}% in 10-25%; "
                "review root cause before deciding on Larger Runners")
    return (f"🚨 HIGH_FRICTION — friction rate {friction_rate:.1f}% > {FRICTION_RATE_REVIEW}%; "
            "consider escalation PR to switch runs-on to ubuntu-latest-4-cores")


def render_markdown(stats: dict, records: list[RunRecord], verbose: bool) -> str:
    """Render the report as Markdown."""
    lines: list[str] = []
    lines.append("# Tier 1 Bench-Gate Friction-Rate Observation (issue #433 W3)")
    lines.append("")
    b = stats["buckets"]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Unique PRs scanned**: {stats['total_unique_prs']}")
    lines.append(f"- **With clear terminal signal** (passed-clean + interventions): {stats['total_with_signal']}")
    lines.append(f"- **Friction denominator**: {stats['friction_denominator']}")
    lines.append(f"- **Friction numerator** (override-applied + merged-despite-red): {stats['friction_count']}")
    lines.append(f"- **Friction rate**: **{stats['friction_rate_pct']}%**")
    lines.append("")
    lines.append("> **⚠️ Friction rate is NOT a strict FP rate.** This tool can only see")
    lines.append("> terminal PR states (latest run + current labels), so any PR where the")
    lines.append("> gate caught a real regression → dev fixed it → green merge is bucketed")
    lines.append("> as `passed-clean`. The metric measures **maintainer-intervention**")
    lines.append("> **frequency**, not true FPs. See module docstring for the survivorship-")
    lines.append("> bias explanation.")
    lines.append("")
    lines.append("## Bucket counts")
    lines.append("")
    lines.append("| Bucket | Count | Meaning | Counts as friction? |")
    lines.append("|---|---|---|---|")
    lines.append(f"| passed-clean | {b['passed-clean']} | Merged + workflow success + no override | NO (clean) |")
    lines.append(f"| override-applied | {b['override-applied']} | Merged + override label present | **YES** |")
    lines.append(f"| merged-despite-red | {b['merged-despite-red']} | Merged + workflow `failure` + no override | **YES** |")
    lines.append(f"| merged-other-state | {b['merged-other-state']} | Merged + workflow cancelled/timed_out/skipped/etc. | excluded (ambiguous) |")
    lines.append(f"| open | {b['open']} | PR still open | excluded |")
    lines.append(f"| unmerged | {b['unmerged']} | PR closed without merge | excluded |")
    lines.append("")
    lines.append("## W3 closure status")
    lines.append("")
    lines.append(f"- Stopping condition: **{stats['stopping_condition']}**")
    lines.append(f"- Recommendation: **{stats['recommendation']}**")
    lines.append("")

    if verbose and records:
        lines.append("## Per-PR detail")
        lines.append("")
        lines.append("| PR | State | Workflow | Bucket | Labels |")
        lines.append("|---|---|---|---|---|")
        for r in records:
            label_str = ", ".join(r.pr_labels) if r.pr_labels else "(none)"
            url = r.pr_url or "?"
            lines.append(
                f"| [#{r.pr_number}]({url}) | {r.pr_state} | "
                f"{r.workflow_conclusion} | `{r.bucket}` | {label_str} |"
            )
        lines.append("")

    lines.append("## Notes / caveats")
    lines.append("")
    lines.append("### Survivorship bias — friction ≠ false positives")
    lines.append("")
    lines.append("This tool can only see terminal states (latest run + current labels).")
    lines.append("A real Tier 1 win — gate catches regression, dev fixes code, green merge —")
    lines.append("looks identical to \"nothing was ever wrong\" from this vantage point. **Caught**")
    lines.append("**bugs are invisible** in the report; only **interventions** are visible.")
    lines.append("")
    lines.append("Consequence: friction rate is an UPPER BOUND on \"unwanted friction\". The")
    lines.append("actual false-positive rate cannot be derived from this tool alone — it would")
    lines.append("require timeline traversal per PR (out of scope for v1).")
    lines.append("")
    lines.append("For W3's \"is the gate annoying enough to escalate to Larger Runners?\" question,")
    lines.append("friction rate IS the right metric: escalation cost is justified by intervention")
    lines.append("frequency regardless of whether each intervention was a true FP or a deliberate")
    lines.append("trade-off. Just don't claim this is measuring \"FP rate\".")
    lines.append("")
    lines.append("### Bucket conflation")
    lines.append("")
    lines.append("- `override-applied` conflates \"true FP override\" with \"deliberate trade-off")
    lines.append("  override\". Both count as friction; both motivate Larger Runners if frequent.")
    lines.append("- `merged-despite-red` could be a real regression maintainer accepted OR a")
    lines.append("  workflow flake (network / timeout) maintainer ignored. If many PRs land here")
    lines.append("  AND raw workflow conclusion shows lots of `failure`, investigate flake source")
    lines.append("  separately (`gh run list --workflow bench-gate-pr.yaml --json conclusion`).")
    lines.append("- `merged-other-state` (cancelled/timed_out/skipped) is **excluded** from the")
    lines.append("  friction denominator — these states are ambiguous as to whether the gate")
    lines.append("  even had an opinion.")
    lines.append("")
    lines.append("### Exclusions")
    lines.append("")
    lines.append("- Open / unmerged PRs are excluded from rate math. If many PRs are stuck open")
    lines.append("  with red bench-gate, that's a separate \"gate as soft blocker\" signal worth")
    lines.append("  noting manually.")
    lines.append("- Run with `--verbose` to inspect per-PR rows.")
    return "\n".join(lines)


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0] if __doc__ else "",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--limit", type=int, default=30,
        help="Number of recent Tier 1 runs to scan (default: 30). "
             "Set higher for more confident FP-rate estimate.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON instead of Markdown (for scripting).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include per-PR detail rows in output.",
    )
    args = parser.parse_args()

    if args.verbose:
        print(f"Querying recent {args.limit} Tier 1 runs...", file=sys.stderr)

    records = collect_records(args.limit, verbose=args.verbose)
    stats = compute_stats(records)

    if args.json:
        # Don't include records in JSON output (verbose can; default keeps tight).
        output = {
            "stats": stats,
            "records": [
                {
                    "pr": r.pr_number,
                    "state": r.pr_state,
                    "conclusion": r.workflow_conclusion,
                    "bucket": r.bucket,
                    "labels": r.pr_labels,
                }
                for r in records
            ] if args.verbose else None,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(stats, records, args.verbose))

    return 0  # observation tool; never gates


if __name__ == "__main__":
    sys.exit(main())
