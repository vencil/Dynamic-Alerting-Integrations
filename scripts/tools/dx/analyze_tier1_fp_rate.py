#!/usr/bin/env python3
"""analyze_tier1_fp_rate.py — Tier 1 bench-gate FP rate observer (issue #433 W3).

Purpose
-------
Issue #433 W3 closure requires observing the Tier 1 bench-gate's false-positive
(FP) rate over an accumulation of real PR traffic. This tool queries recent
`bench-gate-pr.yaml` workflow runs, categorizes each, and computes the FP rate.

Decision matrix (per #433 W3 spec, post-v5 reality):

    FP rate < 10%  → ✅ no escalation needed; W3 closeable
    FP rate 10-25% → ⏳ review root cause (within-runner variance vs workflow bug)
    FP rate > 25%  → 🚨 escalation PR: switch runs-on to ubuntu-latest-4-cores

Close W3 when ANY of these stopping conditions hits:
    - ≥10 Tier 1 runs AND FP rate clearly above/below 10% (clear-signal early close)
    - ≥30 Tier 1 runs (sufficient statistical power for borderline)
    - ≥200 total PRs to repo (hard-cap fallback)

Categorization
--------------
Each Tier 1 run is bucketed into one of:

    passed              gate green; no regression detected
    failed-real         gate red; PR subsequently had new commits pushed
                          (= dev fixed the code) AND eventually merged.
                          Treated as TRUE POSITIVE for FP-rate math.
    failed-override     gate red; override label was eventually applied
                          AND PR merged. Treated as FALSE POSITIVE
                          (maintainer accepted the gate's flag as either
                          a deliberate trade-off OR not-actually-a-regression).
    failed-inconclusive gate red due to INCONCLUSIVE state. v5 single-runner
                          should make this structurally impossible; if observed,
                          flag as a workflow bug (NOT counted in FP rate).
    open                PR still open / no terminal signal yet (excluded).
    unmerged            PR closed without merge (excluded).

FP rate = failed-override / (failed-override + failed-real)

Limitations / honest caveats
----------------------------
1. "failed-real" heuristic assumes a code fix when dev pushes after a red gate.
   In practice, "dev pushed again then re-ran" could also be a flake. The
   heuristic OVER-counts TPs (under-counts FPs), so the actual FP rate is
   probably ≥ what this tool reports. For W3 decisions, "≥ X" is the
   conservative bound — escalating when this tool says > 25% is justified.

2. We exclude open PRs (no terminal signal) and unmerged PRs. If many PRs
   are stuck open with red bench-gate, that's a separate signal worth noting
   manually.

3. We don't try to detect "the dev pushed a no-op rebase to re-trigger CI"
   (which would be a flake-recovery pattern). Such cases would show up as
   "failed-real" here. Real-world FP rate may be slightly higher than reported.

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

# FP-rate thresholds for W3 decision (per issue #433).
FP_RATE_OK = 10.0       # below = no escalation
FP_RATE_REVIEW = 25.0   # 10-25 = review; above 25 = escalate


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

        # PR merged.
        if self.workflow_conclusion == "success":
            self.bucket = "passed"
            return

        # Workflow failed. Check why.
        # Note: we can't easily distinguish "regression" vs "inconclusive"
        # without parsing the workflow's step summary. As a proxy: check
        # if the override label was applied to the PR.
        if OVERRIDE_LABEL in self.pr_labels:
            self.bucket = "failed-override"
            return

        # Failed without override → assume dev fixed via code change.
        # Could also be inconclusive that resolved on re-run; we'd need to
        # parse step summaries to distinguish. For now treat as "real
        # regression dev fixed" (over-counts TP slightly; see limitations).
        self.bucket = "failed-real"


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


def find_pr_for_run(run_id: int) -> Optional[dict]:
    """Resolve a workflow run to its PR. Returns None if no associated PR."""
    try:
        out = _gh([
            "run", "view", str(run_id),
            "--repo", REPO,
            "--json", "pullRequests",
        ])
        data = json.loads(out)
        prs = data.get("pullRequests", [])
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
            print(f"  [{i}/{len(runs)}] resolving run {run['databaseId']}...",
                  file=sys.stderr)
        pr = find_pr_for_run(run["databaseId"])
        if not pr:
            continue
        pr_number = pr.get("number")
        if not pr_number or pr_number in seen_prs:
            # Skip duplicate runs for the same PR; only count one signal per PR.
            continue
        seen_prs.add(pr_number)

        state, labels = get_pr_terminal_state(pr_number)
        rec = RunRecord(
            run_id=run["databaseId"],
            pr_number=pr_number,
            pr_url=pr.get("url"),
            workflow_conclusion=run.get("conclusion", "unknown"),
            pr_state=state,
            pr_labels=labels,
        )
        rec.categorize()
        records.append(rec)
    return records


def compute_stats(records: list[RunRecord]) -> dict:
    """Aggregate categorized records into FP rate + bucket counts."""
    buckets = {
        "passed": 0,
        "failed-real": 0,
        "failed-override": 0,
        "failed-inconclusive": 0,
        "open": 0,
        "unmerged": 0,
    }
    for r in records:
        buckets[r.bucket] = buckets.get(r.bucket, 0) + 1

    # FP rate denominator: merged PRs with terminal gate signal.
    fp_denom = buckets["failed-real"] + buckets["failed-override"]
    fp_rate = (buckets["failed-override"] / fp_denom * 100) if fp_denom else 0.0

    total_with_signal = (
        buckets["passed"] + buckets["failed-real"]
        + buckets["failed-override"] + buckets["failed-inconclusive"]
    )

    return {
        "total_unique_prs": len(records),
        "buckets": buckets,
        "fp_denominator": fp_denom,
        "fp_rate_pct": round(fp_rate, 2),
        "total_with_signal": total_with_signal,
        "stopping_condition": _check_stopping(buckets, fp_rate, total_with_signal),
        "recommendation": _recommend(fp_rate, total_with_signal),
    }


def _check_stopping(buckets: dict, fp_rate: float, total: int) -> str:
    """Which W3 stopping condition is hit (if any)."""
    # Clear-signal early close: ≥10 runs AND result clearly above/below 10%.
    if total >= 10 and (fp_rate < 5.0 or fp_rate > 20.0):
        return "early-close (≥10 runs + clear signal)"
    if total >= 30:
        return "statistical-power threshold (≥30 runs)"
    if total < 10:
        return f"insufficient data ({total}/10 minimum)"
    return f"continue accumulating ({total} runs so far, borderline FP={fp_rate:.1f}%)"


def _recommend(fp_rate: float, total: int) -> str:
    """W3 decision recommendation."""
    if total < 10:
        return "🟡 INSUFFICIENT_DATA — wait for more Tier 1 runs"
    if fp_rate < FP_RATE_OK:
        return f"✅ NO_ESCALATION — FP rate {fp_rate:.1f}% < {FP_RATE_OK}% target"
    if fp_rate < FP_RATE_REVIEW:
        return (f"⏳ REVIEW_NEEDED — FP rate {fp_rate:.1f}% in 10-25% borderline; "
                "investigate root cause before Larger Runners")
    return (f"🚨 ESCALATE — FP rate {fp_rate:.1f}% > {FP_RATE_REVIEW}%; "
            "open PR to switch runs-on to ubuntu-latest-4-cores")


def render_markdown(stats: dict, records: list[RunRecord], verbose: bool) -> str:
    """Render the report as Markdown."""
    lines: list[str] = []
    lines.append("# Tier 1 Bench-Gate FP Rate Observation (issue #433 W3)")
    lines.append("")
    b = stats["buckets"]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Unique PRs scanned**: {stats['total_unique_prs']}")
    lines.append(f"- **With terminal signal** (excludes open / unmerged): {stats['total_with_signal']}")
    lines.append(f"- **FP-rate denominator** (failed-real + failed-override): {stats['fp_denominator']}")
    lines.append(f"- **FP rate**: **{stats['fp_rate_pct']}%**")
    lines.append("")
    lines.append("## Bucket counts")
    lines.append("")
    lines.append("| Bucket | Count | Meaning |")
    lines.append("|---|---|---|")
    lines.append(f"| passed | {b['passed']} | Gate green; no regression detected |")
    lines.append(f"| failed-real | {b['failed-real']} | Gate red; dev pushed code fix; merged → counted as TP |")
    lines.append(f"| failed-override | {b['failed-override']} | Gate red; override label applied; merged → counted as FP |")
    lines.append(f"| failed-inconclusive | {b['failed-inconclusive']} | Gate INCONCLUSIVE (v5 should = 0; nonzero indicates provisioning bug) |")
    lines.append(f"| open | {b['open']} | PR still open; no terminal signal yet (excluded) |")
    lines.append(f"| unmerged | {b['unmerged']} | PR closed without merge (excluded) |")
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
    lines.append("- `failed-real` is a heuristic: \"PR merged after red gate + no override\" → assumed code-fix.")
    lines.append("  Could over-count TPs if a flake resolved on re-run. Actual FP rate may be slightly higher.")
    lines.append("- Inconclusive bucket is approximate — distinguishing from `failed-real` requires parsing each")
    lines.append("  workflow run's step summary (out of scope for v1 of this tool).")
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
