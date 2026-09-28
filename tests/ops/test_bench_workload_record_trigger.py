"""bench-workload-record.yaml's trigger must be exactly the workload closure.

issue #1471 candidate (a). The workflow records the workload-definition effect
of PRs that change only benchmark test code. Its `pull_request.paths` has to be
a LITERAL projection of the closure whose SSOT is
`.github/bench-reference.yaml` → `workload_closure` (GitHub does not evaluate
expressions in `paths`), so the list is duplicated by necessity.

⛔ The failure this pins is silent in exactly one direction: a helper missing
from the trigger means a PR that changes only that helper runs NOTHING — the
pre-#1471 state — and no check anywhere turns red. The workflow's runtime
classifier reads the SSOT, but it only runs once the trigger has already fired,
so it cannot see this. Hence an exact-set comparison here, both directions:
an extra entry is a trigger for something that is not the workload.

Subjects, named in full so a change to either routes back to this file:

    .github/bench-reference.yaml                    — the SSOT
    .github/workflows/bench-workload-record.yaml    — the literal copy
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PIN = ROOT / ".github" / "bench-reference.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "bench-workload-record.yaml"
APP = "components/threshold-exporter/app/"
SELF = ".github/workflows/bench-workload-record.yaml"


def _expected_paths() -> set[str]:
    closure = yaml.safe_load(PIN.read_text(encoding="utf-8"))["workload_closure"]
    glob, helpers = closure["derived_glob"], closure["helpers"]
    assert isinstance(glob, str) and glob, "derived_glob must be a non-empty string"
    assert isinstance(helpers, list) and helpers, (
        "helpers must be a non-empty list — an empty closure would make the "
        "comparison below vacuously about the glob alone")
    # derived_glob is consumed with `find -name` (recursive, basename match), so
    # its projection is `app/**/<glob>`; helpers are paths relative to app/.
    return {APP + "**/" + glob} | {APP + h for h in helpers} | {SELF}


def _trigger_paths() -> list[str]:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    return list(on["pull_request"]["paths"])


def test_trigger_paths_equal_the_workload_closure() -> None:
    got = _trigger_paths()
    assert not [p for p in got if p.startswith("!")], (
        "a `!` exclusion in the trigger is not modelled by this comparison")
    assert len(got) == len(set(got)), f"duplicate trigger entries: {got}"
    expected = _expected_paths()
    missing = sorted(expected - set(got))
    extra = sorted(set(got) - expected)
    assert not missing and not extra, (
        f"{WORKFLOW.relative_to(ROOT)} pull_request.paths disagrees with "
        f"{PIN.relative_to(ROOT)} workload_closure.\n"
        f"  missing from trigger: {missing}\n"
        f"  not in closure:       {extra}\n"
        "⛔ A closure file missing from the trigger is a PR nobody measures, "
        "and nothing else goes red for it.")


def test_every_closure_member_exists_in_the_tree() -> None:
    """Tripwire against the comparison above passing over a dead closure.

    Equality between two lists says nothing if both name files that no longer
    exist (a helper renamed in the tree but not in either list).
    """
    closure = yaml.safe_load(PIN.read_text(encoding="utf-8"))["workload_closure"]
    app = ROOT / APP
    assert sorted(app.rglob(closure["derived_glob"])), (
        f"derived_glob {closure['derived_glob']!r} matches nothing under {APP}")
    gone = [h for h in closure["helpers"] if not (app / h).is_file()]
    assert not gone, f"closure helpers not present under {APP}: {gone}"
