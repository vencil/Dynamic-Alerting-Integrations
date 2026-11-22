"""Every pull_request workflow cancels a superseded run of the same PR.

Runners are the bottleneck of this repo's CI, not execution: a push opens
about 70 jobs, and on GitHub-hosted runners the account-wide concurrency cap
makes most of a PR's wall time QUEUE time (median queue of the required jobs
20-36 min, their execution a few minutes). A workflow without a concurrency
group keeps running for a commit nobody will merge, and every such job holds a
runner slot some current job is waiting for.

So the rule is structural, not a convention to remember: any workflow whose
`on:` has `pull_request` declares a TOP-LEVEL `concurrency`, and, EVALUATED
(the expression evaluator of test_pr_report_workflow_concurrency.py, not a
string match) against synthetic contexts:

  * two runs for the same PR share a group and `cancel-in-progress` is true;
  * runs for two different PRs do not share a group;
  * two different workflows never share a group for the same PR — a shared
    group would let one workflow cancel the other. Groups are compared
    case-insensitively, as GitHub compares them (`CI-101` and `ci-101` are
    one group).

What this deliberately does not decide: how a workflow behaves on push,
schedule or any other event. Main's CI keeping its runs
(test_main_ci_concurrency.py) and the sticky-comment workflows' non-PR rules
(test_pr_report_workflow_concurrency.py) are held where they are argued.

Not shown here: that GitHub honours the group as modelled. The evaluator
mirrors the documented expression semantics; it is not the runner.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pr_report_workflow_concurrency import (  # noqa: E402
    _to_str,
    _triggers,
    render,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _workflows() -> list[Path]:
    found = sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])
    assert found, f"no workflow found under {WORKFLOW_DIR}"
    return found


def _load(path: Path) -> dict:
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(wf, dict), f"{path.name}: not a YAML mapping"
    return wf


def _pr_workflows() -> list[Path]:
    return [p for p in _workflows() if "pull_request" in _triggers(_load(p))]


def _ctx(workflow: str, *, pr: int, run_id: int) -> dict:
    return {
        "workflow": workflow,
        "event_name": "pull_request",
        "run_id": run_id,
        "sha": f"{run_id:040x}",
        "ref": f"refs/pull/{pr}/merge",
        "event": {"pull_request": {"number": pr,
                                   "head": {"sha": f"{run_id:040x}"}}},
    }


def concurrency_problems(name: str, wf: dict) -> list[str]:
    """Problems with one pull_request workflow's concurrency; [] when sound."""
    conc = wf.get("concurrency")
    if isinstance(conc, str):
        conc = {"group": conc}
    if not isinstance(conc, dict) or not conc.get("group"):
        return [f"{name}: no top-level `concurrency` with a `group` — a "
                f"superseded run of the same PR keeps holding runners"]
    wf_name = wf.get("name", name)
    out = []
    a = _ctx(wf_name, pr=101, run_id=1)
    b = _ctx(wf_name, pr=101, run_id=2)
    c = _ctx(wf_name, pr=202, run_id=3)
    ga, gb, gc = (_to_str(render(conc["group"], x)) for x in (a, b, c))
    cancel = render(conc.get("cancel-in-progress", False), b)
    if "${{" not in str(conc["group"]) and cancel is False:
        # A constant group that never cancels is a MUTEX over something
        # shared outside the runner (forge-e2e-github drives one real
        # repository). Cancelling it mid-run would leave that state half
        # written, so it is allowed — but only on a path-gated trigger: a
        # mutex every PR passes through would serialise the whole queue.
        on = wf.get("on", wf.get(True)) or {}
        pr = on.get("pull_request") if isinstance(on, dict) else None
        if not (isinstance(pr, dict) and pr.get("paths")):
            out.append(f"{name}: constant group {ga!r} with no cancel is a "
                       f"global mutex, and its pull_request trigger has no "
                       f"`paths:` — every PR would queue behind it")
        return out
    if ga != gb:
        out.append(f"{name}: two runs of one PR land in different groups "
                   f"({ga!r} / {gb!r}), so the older one is never cancelled")
    if cancel is not True:
        out.append(f"{name}: `cancel-in-progress` renders {cancel!r} on a "
                   f"pull_request — the superseded run keeps running")
    if ga == gc:
        out.append(f"{name}: two different PRs share the group {ga!r} — one "
                   f"PR's push would cancel another PR's run")
    return out


def test_the_scan_sees_the_workflows() -> None:
    # Anchors the population: a glob that silently matched nothing would make
    # every parametrized case below vanish and the module pass vacuously.
    names = {p.name for p in _pr_workflows()}
    assert {"ci.yml", "docs-ci.yaml", "validate.yaml",
            "commitlint.yaml"} <= names, sorted(names)


@pytest.mark.parametrize("path", _pr_workflows(), ids=lambda p: p.name)
def test_a_pull_request_workflow_cancels_its_superseded_run(path) -> None:
    problems = concurrency_problems(path.name, _load(path))
    assert not problems, "\n".join(problems)


def shared_groups(workflows: list[tuple[str, dict]]) -> list[str]:
    """Pairs of workflows whose groups for one PR coincide. PURE.

    GitHub compares concurrency group names case-insensitively, so the
    comparison here folds case: `CI-101` and `ci-101` are one group."""
    seen: dict[str, str] = {}
    clashes = []
    for name, wf in workflows:
        conc = wf.get("concurrency")
        group = conc if isinstance(conc, str) else (conc or {}).get("group")
        if not group:
            continue  # reported by the per-workflow test
        g = _to_str(render(group, _ctx(wf.get("name", name),
                                         pr=101, run_id=1))).casefold()
        if g in seen:
            clashes.append(f"{seen[g]} and {name} share {g!r}")
        seen[g] = name
    return clashes


def test_no_two_workflows_share_a_group_for_one_pr() -> None:
    clashes = shared_groups([(p.name, _load(p)) for p in _pr_workflows()])
    assert not clashes, "\n".join(clashes)


@pytest.mark.parametrize("a, b, expected", [
    ("CI-${{ github.event.pull_request.number }}",
     "ci-${{ github.event.pull_request.number }}", 1),   # case-only clash
    ("ci-${{ github.event.pull_request.number }}",
     "docs-${{ github.event.pull_request.number }}", 0),  # distinct
])
def test_the_shared_group_finder_folds_case(a, b, expected) -> None:
    wfs = [("a.yml", {"name": "a", "concurrency": {"group": a}}),
           ("b.yml", {"name": "b", "concurrency": {"group": b}})]
    assert len(shared_groups(wfs)) == expected


@pytest.mark.parametrize("conc, expected", [
    (None, 1),                                                  # missing
    ({"group": "w-${{ github.event.pull_request.number }}"}, 1),  # no cancel
    ({"group": "w", "cancel-in-progress": True}, 1),             # PRs share
    ({"group": "w-${{ github.run_id }}", "cancel-in-progress": True}, 1),
    ({"group": "w-${{ github.event.pull_request.number }}",
      "cancel-in-progress": "${{ github.event_name == 'pull_request' }}"}, 0),
    ({"group": "w-${{ github.ref }}", "cancel-in-progress": True}, 0),
    ({"group": "w", "cancel-in-progress": False}, 1),           # mutex, ungated
], ids=["missing", "no-cancel", "prs-share", "per-run-group", "sound",
        "sound-by-ref", "mutex-on-every-pr"])
def test_the_rule_can_fail(conc, expected) -> None:
    wf = {"name": "w", "on": {"pull_request": {}}}
    if conc is not None:
        wf["concurrency"] = conc
    assert len(concurrency_problems("w.yml", wf)) == expected


def test_a_path_gated_mutex_is_allowed() -> None:
    wf = {"name": "w", "on": {"pull_request": {"paths": ["x/**"]}},
          "concurrency": {"group": "w", "cancel-in-progress": False}}
    assert concurrency_problems("w.yml", wf) == []
