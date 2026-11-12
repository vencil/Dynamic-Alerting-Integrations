"""Main's CI runs finish; PR runs are superseded.

Branch protection does not require a PR to be up to date with main, so the
run on main is the only check of the merged result. With
`cancel-in-progress: true` on every event, each merge cancelled the run of the
merge before it, and a full run takes longer than the gap between merges:
on 2026-11-10 the CI of five consecutive main commits was cancelled before
one finished.

What this module holds, by EVALUATING the workflow expressions (the evaluator
is the one test_pr_report_workflow_concurrency.py already models GitHub's
expression semantics with — not a string match):

  * a pull_request cancels the older run of the same PR; a push to main never
    cancels, and two pushes to main share one group, so GitHub keeps one run
    going and replaces only the PENDING one — the main tip always gets a run
    that finishes;
  * nothing sets LINT_DIFF_BASE: on a push the diff-aware lints resolve to an
    empty diff (merge base of origin/main and HEAD is HEAD, even after newer
    merges), which is what a PR-scoped check should do on main.

A commit whose PENDING run was replaced keeps a cancelled status; the next
run's full-tree checks and tests cover its content.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pr_report_workflow_concurrency import _to_str, render  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/docs-ci.yaml")
_BEFORE = "b" * 40


def _load(rel: str) -> dict:
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def _ctx(event: str, *, pr: int | None = None, before: str = _BEFORE) -> dict:
    ref = f"refs/pull/{pr}/merge" if pr is not None else "refs/heads/main"
    payload: dict = {"before": before}  # synchronize carries `before` too
    if pr is not None:
        payload["pull_request"] = {"number": pr}
    return {"event_name": event, "ref": ref, "event": payload}


def _group_and_cancel(rel: str, ctx: dict) -> tuple[str, bool]:
    conc = _load(rel)["concurrency"]
    cancel = render(conc.get("cancel-in-progress", False), ctx)
    assert isinstance(cancel, bool), f"{rel}: cancel-in-progress rendered {cancel!r}"
    return _to_str(render(conc["group"], ctx)), cancel


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_both_workflows_run_on_push_to_main_and_pull_request(rel) -> None:
    """Anti-vacuity: the cases below are the events these workflows run on."""
    wf = _load(rel)
    on = wf.get("on", wf.get(True))
    assert on["push"]["branches"] == ["main"], rel
    assert "pull_request" in on, rel


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_a_push_to_main_never_cancels_and_pushes_share_a_group(rel) -> None:
    g1, c1 = _group_and_cancel(rel, _ctx("push", before="1" * 40))
    g2, c2 = _group_and_cancel(rel, _ctx("push", before="2" * 40))
    assert c1 is False and c2 is False, (
        f"{rel}: a push to main renders cancel-in-progress true, so each merge "
        f"cancels the previous merge's run and main has no finished CI while "
        f"merges are closer together than one run.")
    assert g1 == g2, (
        f"{rel}: two pushes to main land in different groups ({g1!r}, {g2!r}); "
        f"every main commit would then run at once and fill the runners.")


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_a_newer_push_to_a_pr_still_cancels_its_older_run(rel) -> None:
    g1, c1 = _group_and_cancel(rel, _ctx("pull_request", pr=7))
    g2, _ = _group_and_cancel(rel, _ctx("pull_request", pr=7))
    g3, _ = _group_and_cancel(rel, _ctx("pull_request", pr=8))
    assert c1 is True, f"{rel}: a PR's superseded run is no longer cancelled"
    assert g1 == g2 and g1 != g3, (g1, g2, g3)


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_no_level_sets_a_push_time_diff_base(rel) -> None:
    """Diff-aware lints are PR checks: their exemptions are read from the PR
    body (PR_BODY), which a push to main does not have. A push-time
    LINT_DIFF_BASE (e.g. `github.event.before`) would make them re-judge a
    merged change without its exemption and redden main for a change the PR
    deliberately exempted (independent review, measured with
    `bypass-lint: codename-leak`). Unset, they resolve to an empty diff on a
    push (tests/lint/test_diff_scan_failure.py)."""
    wf = _load(rel)
    found = ["env"] if "LINT_DIFF_BASE" in (wf.get("env") or {}) else []
    for name, job in (wf.get("jobs") or {}).items():
        if "LINT_DIFF_BASE" in (job.get("env") or {}):
            found.append(f"jobs.{name}.env")
        for i, step in enumerate(job.get("steps") or []):
            if "LINT_DIFF_BASE" in (step.get("env") or {}):
                found.append(f"jobs.{name}.steps[{i}].env")
    assert not found, f"{rel}: LINT_DIFF_BASE set at {found}"
