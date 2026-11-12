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
    going and replaces only the PENDING one — every commit is covered by a run
    that finishes;
  * LINT_DIFF_BASE renders to `github.event.before` on a push and to '' on a
    pull_request. A main run that is no longer cancelled can start a job after
    newer merges, and the diff-aware lints would otherwise take their base from
    whatever origin/main is by then (tests/lint/test_diff_scan_failure.py shows
    what that base misses).
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
def test_lint_diff_base_is_the_replaced_main_tip_on_push_only(rel) -> None:
    env = _load(rel).get("env") or {}
    assert "LINT_DIFF_BASE" in env, (
        f"{rel}: no workflow-level LINT_DIFF_BASE. A main run that is not "
        f"cancelled can start a job after newer merges and then take its diff "
        f"base from that newer origin/main.")
    on_push = render(env["LINT_DIFF_BASE"], _ctx("push"))
    on_pr = render(env["LINT_DIFF_BASE"], _ctx("pull_request", pr=7))
    assert on_push == _BEFORE, f"{rel}: on push LINT_DIFF_BASE renders {on_push!r}"
    # '' is read as unset by _lint_helpers.resolve_diff_base_ref and by
    # check_account_registry_monotonic, so a PR keeps origin/$GITHUB_BASE_REF
    # (a pull_request `synchronize` payload carries `before` as well).
    assert on_pr == "", f"{rel}: on pull_request LINT_DIFF_BASE renders {on_pr!r}"


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_no_job_or_step_overrides_lint_diff_base(rel) -> None:
    """One place sets it; a job-level copy would quietly win over it."""
    wf = _load(rel)
    found = []
    for name, job in (wf.get("jobs") or {}).items():
        if "LINT_DIFF_BASE" in (job.get("env") or {}):
            found.append(f"jobs.{name}.env")
        for i, step in enumerate(job.get("steps") or []):
            if "LINT_DIFF_BASE" in (step.get("env") or {}):
                found.append(f"jobs.{name}.steps[{i}].env")
    assert not found, f"{rel}: LINT_DIFF_BASE also set at {found}"
