"""Every Trivy scan in CI uses the same finding filter (#3870).

release.yaml blocks on findings, nightly-image-scan.yaml reports them,
component-docker-build.yaml shows them on the PR. Those three only predict
each other if they agree on what a finding IS — `severity` and
`ignore-unfixed`, kept once in tests/_trivy_contract.py. Before this test the
PR-time workflow said "same severity/ignore-unfixed contract as release.yaml"
in a comment and nothing checked it.

Discovery is by parsing every workflow and every step that `uses:`
aquasecurity/trivy-action, so a new scan anywhere joins the contract without
being listed. A workflow that fails to parse is a failure, not a skip.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from _trivy_contract import SCAN_FILTER, filter_mismatches

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
_TRIVY_ACTION = "aquasecurity/trivy-action"


def _workflows() -> dict[str, dict]:
    return {
        p.name: yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for p in sorted(WORKFLOWS_DIR.glob("*.y*ml"))
    }


def trivy_steps(workflows: dict[str, dict]) -> list[tuple[str, dict]]:
    found = []
    for wf_name, wf in workflows.items():
        for job_name, job in (wf.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                if str(step.get("uses") or "").startswith(_TRIVY_ACTION):
                    found.append((f"{wf_name}::{job_name}", step))
    return found


def check(workflows: dict[str, dict]) -> list[str]:
    steps = trivy_steps(workflows)
    if len({where.split("::")[0] for where, _ in steps}) < 3:
        return [f"found Trivy steps in fewer than 3 workflows ({[w for w, _ in steps]}) — discovery drifted"]
    return [
        f"{where}: {m}"
        for where, step in steps
        for m in filter_mismatches(step.get("with") or {})
    ]


def test_every_trivy_scan_shares_the_finding_filter() -> None:
    problems = check(_workflows())
    assert not problems, (
        "these Trivy steps disagree with tests/_trivy_contract.py SCAN_FILTER "
        f"{SCAN_FILTER}:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("workflow", [
    "release.yaml", "nightly-image-scan.yaml", "component-docker-build.yaml",
])
@pytest.mark.parametrize("key,value", [
    ("severity", "CRITICAL"), ("ignore-unfixed", False), ("format", "sarif"), ("format", "SARIF"),
])
def test_a_drifted_consumer_is_named(workflow: str, key: str, value) -> None:
    wfs = copy.deepcopy(_workflows())
    where, step = next((w, s) for w, s in trivy_steps({workflow: wfs[workflow]}))
    step["with"][key] = value
    problems = check(wfs)
    assert any(p.startswith(f"{where}: {key}=") for p in problems), problems
