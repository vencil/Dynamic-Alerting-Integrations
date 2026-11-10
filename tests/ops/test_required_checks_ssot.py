"""`.github/required-checks.yaml` is the one source for main's required checks.

#3696: "Smoke Tests (Chromium)" was required in GitHub's settings while the
workflow reporting it had `on.pull_request.paths`, so every PR touching no
portal path sat at `blocked` — and that workflow's comment said the job was not
required. Nothing in the repo knew the list. This module checks the file
against the WORKFLOWS (static, every PR); `required-checks-drift.yml` checks it
against the live SETTINGS.

What a `required` entry must satisfy, all mechanically:

  * exactly one job in all of `.github/workflows/` can report the name (a
    second job with the same name — any trigger — could satisfy the check);
  * that job's workflow starts on every pull request to main: a
    `pull_request` trigger with no `paths` / `paths-ignore`, `branches` absent
    or including main, `types` absent or including opened/synchronize/reopened;
  * `kind: gate` — the job is an aggregate gate: `if: always()` and `needs`.
    Its decision logic is test_ci_aggregate_gate_contract.py's business, and
    REQUIRED_CHECK_NAMES there is derived from these entries;
  * `kind: job` — the job cannot be skipped: no `needs` (a failed dependency
    SKIPS it, and a skipped check counts as success), no `if:` other than
    `always()`, no matrix, no `continue-on-error`.

`advisory` entries are checks that deliberately do not block merge, each with
its reason: they must exist, carry a reason, and never appear in `required`.

⚠️ Not covered here: whether the file matches GitHub's settings (that is the
drift workflow), and a job whose `name:` holds an expression is matched by its
literal fragments only — the same rendering limit the aggregate-gate module
documents.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SSOT = ROOT / ".github/required-checks.yaml"
WORKFLOW_DIR = ROOT / ".github/workflows"
DRIFT_SCRIPT = ROOT / "scripts/ops/required_checks_drift.py"

_PR_TYPES_NEEDED = {"opened", "synchronize", "reopened"}


def _ssot() -> dict:
    return yaml.safe_load(SSOT.read_text(encoding="utf-8"))


def _on(doc: dict):
    """`on:` — YAML 1.1 reads the bare key as the boolean True."""
    return doc.get("on", doc.get(True))


def _workflows() -> list[tuple[str, dict]]:
    return [(p.name, yaml.safe_load(p.read_text(encoding="utf-8")))
            for p in sorted(WORKFLOW_DIR.glob("*.y*ml"))]


def _name_pattern(raw: str) -> re.Pattern:
    """A job name with `${{ … }}` matches anything in place of the expression."""
    parts = re.split(r"\$\{\{.*?\}\}", raw)
    return re.compile(".*".join(re.escape(p) for p in parts) + r"\Z", re.S)


def could_report(raw: str, job: dict, name: str) -> bool:
    """PURE. Whether a job whose `name:` is `raw` can produce the check `name`.

    ⛔ A matrix job whose name holds no `matrix.` expression gets a suffix
    from GitHub: `name (cell values)`. Blind review: `name: Smoke Tests` with
    `matrix.browser: [Chromium]` reports `Smoke Tests (Chromium)` and was
    invisible here. Any suffix is matched, not just the listed cells — an
    `include:` can add values this module would have to evaluate."""
    if (job.get("strategy") or {}).get("matrix") and "matrix." not in raw:
        # Always suffixed: such a job never reports its bare name.
        return re.match(re.escape(raw) + r" \(.*\)\Z", name, re.S) is not None
    return raw == name or ("${{" in raw and _name_pattern(raw).match(name) is not None)


def _reporters(name: str) -> list[tuple[str, str, dict, dict]]:
    """(workflow file, job id, job, workflow) for every job that could report `name`."""
    found = []
    for wf_name, doc in _workflows():
        for job_id, job in (doc.get("jobs") or {}).items():
            if could_report(str(job.get("name", job_id)), job, name):
                found.append((wf_name, job_id, job, doc))
    return found


def trigger_problems(wf_name: str, doc: dict) -> list[str]:
    """PURE. Why this workflow might not start on a pull request to main."""
    on = _on(doc)
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {event: None for event in on}
    if not isinstance(on, dict) or "pull_request" not in on:
        return [f"{wf_name} has no `pull_request` trigger"]
    pr = on["pull_request"] or {}
    problems = [f"{wf_name}: `on.pull_request.{key}` — a pull request outside it "
                "never starts the workflow, so the check never reports and the PR "
                "is held at `blocked`"
                for key in ("paths", "paths-ignore") if pr.get(key)]
    branches = pr.get("branches")
    if branches is not None and "main" not in branches:
        problems.append(f"{wf_name}: `on.pull_request.branches` {branches} does not include main")
    if pr.get("branches-ignore"):
        problems.append(f"{wf_name}: `on.pull_request.branches-ignore` is set")
    types = pr.get("types")
    if types is not None and not _PR_TYPES_NEEDED <= set(types):
        problems.append(f"{wf_name}: `on.pull_request.types` {types} lacks "
                        f"{sorted(_PR_TYPES_NEEDED - set(types))}")
    return problems


def job_problems(where: str, job: dict, kind: str) -> list[str]:
    """PURE. Why this job might not report, or might report a skip as success."""
    cond = str(job.get("if", "")).strip()
    if kind == "gate":
        if not job.get("needs") or not cond.startswith("always()"):
            return [f"{where} is `kind: gate` but is not an aggregate gate "
                    "(`if: always()` with `needs:`)"]
        return []
    if kind != "job":
        return [f"{where}: unknown kind {kind!r} (gate | job)"]
    problems = []
    if job.get("needs"):
        problems.append(f"{where} has `needs:` — a failed dependency SKIPS it, and "
                        "a skipped required check counts as success")
    if cond and cond != "always()":
        problems.append(f"{where} has `if: {cond}` — a skip counts as success")
    if (job.get("strategy") or {}).get("matrix"):
        problems.append(f"{where} is a matrix job — its check names are per cell")
    if job.get("continue-on-error"):
        problems.append(f"{where} has `continue-on-error` — it reports success "
                        "over a failure")
    return problems


def reporter_count_problem(name: str, where: list[tuple[str, str]]) -> str | None:
    """PURE. Exactly one job may report a required name: none ⇒ it never
    reports (the PR is held at `blocked`); two ⇒ either one can satisfy it."""
    if len(where) != 1:
        return f"{name!r}: {len(where)} jobs can report it ({where}) — exactly one must"
    return None


def test_every_required_check_is_reported_by_a_job_that_always_reports() -> None:
    entries = _ssot()["required"]
    assert len(entries) >= 20, (
        f"{len(entries)} required entries — fewer than the 20 read from branch "
        "protection on 2026-11-10; if checks were removed, lower this floor in the "
        "same change, so an emptied file cannot pass vacuously")
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names)), f"duplicate names: {names}"
    problems = []
    for entry in entries:
        name, kind = entry["name"], entry.get("kind")
        reporters = _reporters(name)
        count = reporter_count_problem(name, [(w, j) for w, j, _, _ in reporters])
        if count:
            problems.append(count)
            continue
        wf_name, job_id, job, doc = reporters[0]
        problems += trigger_problems(wf_name, doc)
        problems += job_problems(f"{wf_name}::{job_id} ({name!r})", job, kind)
        if set(entry) - {"name", "kind", "pending"}:
            problems.append(f"{name!r}: unknown keys {sorted(set(entry) - {'name', 'kind', 'pending'})}")
    assert not problems, "\n  ".join([""] + problems)


def test_advisory_checks_exist_carry_a_reason_and_are_not_required() -> None:
    ssot = _ssot()
    required = {e["name"] for e in ssot["required"]}
    problems = []
    for entry in ssot.get("advisory") or []:
        name = entry.get("name")
        if not str(entry.get("reason") or "").strip():
            problems.append(f"advisory {name!r} has no reason")
        if name in required:
            problems.append(f"{name!r} is both required and advisory")
        if not _reporters(name):
            problems.append(f"advisory {name!r} is reported by no job — renamed or "
                            "deleted? A row that outlives its job hides the next one")
    assert not problems, "\n  ".join([""] + problems)


@pytest.mark.parametrize("on, expected", [
    ({"pull_request": {"branches": ["main"]}}, 0),
    ({"pull_request": None}, 0),
    ("pull_request", 0),
    (["push", "pull_request"], 0),
    ({"pull_request": {"types": ["opened", "synchronize", "reopened", "edited"]}}, 0),
    ({"pull_request": {"paths": ["tools/**"]}}, 1),
    ({"pull_request": {"paths-ignore": ["docs/**"]}}, 1),
    ({"pull_request": {"branches": ["develop"]}}, 1),
    ({"pull_request": {"branches-ignore": ["main"]}}, 1),
    ({"pull_request": {"types": ["opened"]}}, 1),
    ({"push": {"branches": ["main"]}}, 1),
], ids=["branches", "bare", "string", "list", "types-ok", "paths", "paths-ignore",
        "other-branch", "branches-ignore", "types-short", "no-pr"])
def test_the_trigger_rule_can_fail(on, expected) -> None:
    assert len(trigger_problems("w.yml", {True: on})) == expected


@pytest.mark.parametrize("job, kind, expected", [
    ({}, "job", 0),
    ({"if": "always()"}, "job", 0),
    ({"needs": "x"}, "job", 1),
    ({"if": "github.event_name == 'pull_request'"}, "job", 1),
    ({"strategy": {"matrix": {"a": [1]}}}, "job", 1),
    ({"continue-on-error": True}, "job", 1),
    ({"needs": ["d", "r"], "if": "always()"}, "gate", 0),
    ({"needs": ["d", "r"]}, "gate", 1),
    ({"if": "always()"}, "gate", 1),
    ({}, "other", 1),
], ids=["plain", "always", "needs", "if", "matrix", "coe", "gate", "gate-no-always",
        "gate-no-needs", "bad-kind"])
def test_the_job_rule_can_fail(job, kind, expected) -> None:
    assert len(job_problems("w::j", job, kind)) == expected


# ── the drift script (required-checks-drift.yml) ─────────────────────────

def _drift():
    spec = importlib.util.spec_from_file_location("required_checks_drift", DRIFT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _branch(checks: dict[str, int], level: str = "everyone") -> dict:
    return {"protection": {"enabled": True, "required_status_checks": {
        "enforcement_level": level,
        "checks": [{"context": c, "app_id": a} for c, a in checks.items()]}}}


_FILE = {"app_id": 15368, "enforcement_level": "everyone",
         "required": [{"name": "A", "kind": "job"}, {"name": "B", "kind": "gate"},
                      {"name": "P", "kind": "job", "pending": True}]}


@pytest.mark.parametrize("live, level, expected", [
    ({"A": 15368, "B": 15368}, "everyone", []),
    ({"A": 15368}, "everyone", ["'B' is required in the file but not"]),
    ({"A": 15368, "B": 15368, "C": 15368}, "everyone", ["'C' is required in branch protection but not"]),
    ({"A": 15368, "B": 15368, "P": 15368}, "everyone", ["'P' is enabled in branch protection but still"]),
    ({"A": 15368, "B": 99}, "everyone", ["'B' is bound to app_id 99"]),
    ({"A": 15368, "B": 15368}, "non_admins", ["enforcement_level is 'non_admins'"]),
], ids=["match", "missing-live", "extra-live", "stale-pending", "other-app", "level"])
def test_drift_names_every_difference(live, level, expected) -> None:
    problems = _drift().compare(_FILE, _branch(live, level))
    assert len(problems) == len(expected), problems
    for want, got in zip(expected, problems):
        assert got.startswith(want), (want, got)


@pytest.mark.parametrize("branch", [
    {}, {"protection": None}, {"protection": {"enabled": True}},
    {"protection": {"required_status_checks": {"contexts": ["A"]}}},
    {"protection": {"required_status_checks": {"checks": ["A"]}}},
], ids=["no-protection", "null", "no-rsc", "no-checks", "bad-entry"])
def test_drift_refuses_an_answer_it_cannot_read(branch) -> None:
    """⛔ A token that cannot see the field must never read as 'nothing required'."""
    mod = _drift()
    with pytest.raises(mod.Unreadable):
        mod.compare(_FILE, branch)


def test_drift_script_exit_codes(tmp_path) -> None:
    import json
    mod = _drift()
    ssot = tmp_path / "ssot.yaml"
    ssot.write_text(yaml.safe_dump(_FILE), encoding="utf-8")
    cases = [(_branch({"A": 15368, "B": 15368}), 0),
             (_branch({"A": 15368}), 1),
             ({"protection": {}}, 2)]
    for branch, rc in cases:
        path = tmp_path / "b.json"
        path.write_text(json.dumps(branch), encoding="utf-8")
        assert mod.main([str(path), str(ssot)]) == rc, branch


def test_the_repo_file_parses_under_the_drift_script() -> None:
    """The drift script and this module read the same file the same way."""
    names, app_id, level = _drift().declared(_ssot())
    assert app_id == 15368 and level == "everyone", (app_id, level)
    assert set(names) == {e["name"] for e in _ssot()["required"]}


# ── prose about required status lives in the file, not in workflow comments ──

# A comment paragraph that states a check is NOT required. #3696's shape was
# exactly such a sentence ("This job stays ADVISORY (not a required check)")
# outliving the setting it described. Paragraphs = runs of comment lines,
# folded, so a claim wrapped across lines is still seen; a bare `#` line or a
# non-comment line ends a paragraph.
_NOT_REQUIRED = re.compile(
    r"(?i)\b(?:does\s*not|doesn't|never|no\s+longer|won't|will\s+not)\s+blocks?\s+"
    r"(?:the\s+|a\s+|any\s+)?(?:pr\s+)?merges?\b"
    r"|\b(?:not|isn't|non)[\s-]+(?:(?:a|one\s+of(?:\s+main's|\s+the)?|among(?:\s+the|"
    r"\s+main's)?|in(?:\s+main's|\s+the)?|marked|listed\s+as(?:\s+a)?)\s+)*"
    r"(?:branch-protection\s+)?required(?:[\s-]+status)?[\s-]+checks?\b"
    r"|\bnot\s+marked\s+required\b|\bstays\s+advisory\b|\bthis\s+job\s+is\s+not\s+required\b")


def _comment_paragraphs(text: str):
    para, start = [], 0
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        body = stripped.lstrip("#").strip() if stripped.startswith("#") else None
        # A bare `#` line separates paragraphs too: without that, a whole file
        # header is one "paragraph" and one citation anywhere in it clears
        # every claim in it.
        if body:
            if not para:
                start = i
            para.append(body)
        elif para:
            yield start, " ".join(para)
            para = []
    if para:
        yield start, " ".join(para)


def test_workflow_prose_about_required_status_points_at_the_file() -> None:
    problems = [
        f"{p.name}:{line}: {para[:110]}…"
        for p in sorted(WORKFLOW_DIR.glob("*.y*ml"))
        for line, para in _comment_paragraphs(p.read_text(encoding="utf-8"))
        if _NOT_REQUIRED.search(para) and "required-checks.yaml" not in para
    ]
    assert not problems, (
        "these comments state that a check is not required without pointing at "
        ".github/required-checks.yaml. Such a sentence goes stale the moment the "
        "settings change (#3696). Put the reason under `advisory:` in that file and "
        "cite the file here:\n  " + "\n  ".join(problems))


@pytest.mark.parametrize("text, hits", [
    ("# This job stays ADVISORY (not a required check).", 1),
    ("# ⚠️ THIS JOB IS NOT A REQUIRED STATUS CHECK (measured …)", 1),
    ("# this job is NOT\n# in main's required status checks", 1),
    ("# is not among the required status checks on main", 1),
    ("# Not a required check. rc 2 fails the job.", 1),
    ("# NOT in the branch-protection required-check list", 1),
    ("# not a required check — see .github/required-checks.yaml", 0),
    ("# the REQUIRED check is the aggregate gate below", 0),
    # blind review: spellings the first version missed
    ("# status appears but doesn't block merge", 1),
    ("# The gate no longer blocks merge.", 1),
    ("# comment + label, never blocks merge", 1),
    ("# this job is not required.", 1),
    ("# isn't a required check", 1),
    ("# non-required check", 1),
    ("# not marked required in branch protection", 1),
    ("# is not listed as a required status check", 1),
], ids=["stays-advisory", "upper", "wrapped", "among", "plain", "list", "cited", "positive",
        "doesnt-block", "no-longer", "never", "job-not-required", "isnt", "non-required",
        "not-marked", "not-listed"])

def test_the_prose_rule_sees_the_spellings_in_use(text, hits) -> None:
    found = [p for _l, p in _comment_paragraphs(text)
             if _NOT_REQUIRED.search(p) and "required-checks.yaml" not in p]
    assert len(found) == hits, (text, found)


@pytest.mark.parametrize("raw, job, name, expected", [
    ("Smoke Tests", {"strategy": {"matrix": {"browser": ["Chromium"]}}}, "Smoke Tests (Chromium)", True),
    ("Smoke Tests", {}, "Smoke Tests (Chromium)", False),
    ("Smoke ${{ matrix.b }}", {"strategy": {"matrix": {"b": ["x"]}}}, "Smoke Tests (Chromium)", True),
    ("Lint", {}, "Lint", True),
    ("Lint", {"strategy": {"matrix": {"a": [1]}}}, "Lint", False),
], ids=["matrix-suffix", "no-matrix", "expression", "exact", "matrix-renames"])
def test_the_reporter_rule_sees_matrix_suffixes(raw, job, name, expected) -> None:
    assert could_report(raw, job, name) is expected


@pytest.mark.parametrize("where, bad", [
    ([], True), ([("a.yml", "x")], False), ([("a.yml", "x"), ("b.yml", "y")], True),
], ids=["none", "one", "two"])
def test_the_reporter_count_rule_can_fail(where, bad) -> None:
    """Negative control (mutation: `!= 1` → `< 1` stayed green, because the
    tree has no second reporter for any required name today)."""
    assert (reporter_count_problem("N", where) is not None) is bad


def test_the_gate_contract_reads_the_gate_entries_of_this_file() -> None:
    """REQUIRED_CHECK_NAMES in the aggregate-gate contract is DERIVED from the
    `kind: gate` entries. Mutation: deriving it as an empty set left that whole
    module green — every assertion there iterates the set, so empty is vacuous."""
    spec = importlib.util.spec_from_file_location(
        "agg_contract", ROOT / "tests/ops/test_ci_aggregate_gate_contract.py")
    agg = importlib.util.module_from_spec(spec)
    # Registered first: the module's dataclasses resolve their module by name.
    sys.modules[spec.name] = agg
    try:
        spec.loader.exec_module(agg)
    finally:
        sys.modules.pop(spec.name, None)
    gates = {e["name"] for e in _ssot()["required"] if e.get("kind") == "gate"}
    assert gates, "no `kind: gate` entries — the gate contract would check nothing"
    assert agg.REQUIRED_CHECK_NAMES == gates, (agg.REQUIRED_CHECK_NAMES, gates)
