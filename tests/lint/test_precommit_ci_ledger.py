"""Every auto-stage pre-commit hook must declare where CI runs it (#2694).

A hook the CI never runs is enforced only where a contributor's local hook
happens to fire. #2644 inventoried the hooks no workflow names and classified
each one by hand; this module turns that one-off inventory into a rule that a
NEW hook has to satisfy too. An auto-stage hook in `.pre-commit-config.yaml`
must either

  * be called BY ID — `pre-commit run <id> ...` — in a step's `run:` script
    under `.github/workflows/`, or
  * have an entry in `tests/lint/precommit-ci-ledger.yaml` naming its CI
    execution point: (a) another job, (b) pytest node ids, or `exempt` + why.

And the ledger may not rot: an entry must name a hook that still exists, is
still auto-stage, is NOT already called by id (the entry would be dead weight),
and whose evidence resolves — the workflow file has that job, every pytest
file exists and every `::Name` in a node id is in that file's AST.

⛔ Workflows are PARSED, never grepped: `ci.yml` comments name hook ids. Each
step's `run:` goes through the shared `_logical_shell_lines` (comment lines
dropped, continuations joined — imported via `tests/_ci_lint_wiring.py`, not
re-implemented), then a trailing `# ...` comment is cut and
`pre-commit run\\s+([^\\s-][^\\s]*)` names the hook. The first-character class
skips flags, so `pre-commit run --all-files` names nothing.

"Auto stage" follows pre-commit's own resolution: the hook's `stages:`, else
the top-level `default_stages:`, else every stage. A hook is auto when the
result contains `pre-commit` (or the legacy alias `commit`).

⛔ Fail-closed: a ledger that does not parse, or whose shape is off (unknown
key or category, missing evidence, duplicate id), raises `LedgerError` instead
of being read as "no entries". A workflow that is not a mapping raises too.

⚠️ What this does NOT measure: that the (a) job or the (b) tests still RED on
a violation the hook rejects (measured once per entry when #2644 planted one),
that an (a) job is unconditional (go-fmt's job is path-gated on purpose), or
that a by-id call is itself a gate (`tests/_ci_lint_wiring.py` pins that for
the hooks listed in `tests/lint/test_ci_lint_hook_entries.py`).
"""
from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from _ci_lint_wiring import _logical_shell_lines
from _pysource import parse_py
from _tree import REPO_ROOT, repo_files

PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
LEDGER = REPO_ROOT / "tests" / "lint" / "precommit-ci-ledger.yaml"
WORKFLOW_DIR = ".github/workflows"

HOOK_CALL = re.compile(r"pre-commit run\s+([^\s-][^\s]*)")
TRAILING_COMMENT = re.compile(r"(^|\s)#.*$")
AUTO_STAGES = {"pre-commit", "commit"}

EVIDENCE_KEYS = {
    "a": {"workflow", "job", "note"},
    "b": {"pytest"},
    "exempt": {"reason"},
}


class LedgerError(ValueError):
    """The ledger cannot be read as a ledger. Never treated as 'empty'."""


# ── pure functions (fed synthetic input by the table tests below) ────────────


def auto_stage_hooks(config: Mapping) -> list[str]:
    """Ids of the hooks pre-commit runs at the `pre-commit` stage."""
    default = config.get("default_stages")
    out = []
    for repo in config.get("repos") or []:
        for hook in repo.get("hooks") or []:
            stages = hook.get("stages", default)
            if stages is None or AUTO_STAGES & set(stages):
                out.append(hook["id"])
    return out


def all_hooks(config: Mapping) -> set[str]:
    return {h["id"] for r in config.get("repos") or [] for h in r.get("hooks") or []}


def hooks_called_by_id(workflows: Mapping[str, object]) -> dict[str, list[str]]:
    """hook id -> ["<workflow>::<job>", ...] for every by-id `pre-commit run`."""
    called: dict[str, list[str]] = {}
    for name, workflow in sorted(workflows.items()):
        if not isinstance(workflow, Mapping) or not isinstance(
                workflow.get("jobs"), Mapping):
            raise LedgerError(f"{name}: not a workflow mapping with `jobs:`")
        for job_id, job in workflow["jobs"].items():
            for step in (job or {}).get("steps") or []:
                for line in _logical_shell_lines(str(step.get("run") or "")):
                    line = TRAILING_COMMENT.sub("", line)
                    for match in HOOK_CALL.finditer(line):
                        called.setdefault(match.group(1), []).append(
                            f"{name}::{job_id}")
    return called


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_ledger(text: str) -> dict[str, dict]:
    """hook id -> entry. Raises LedgerError on anything but the exact shape."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LedgerError(f"ledger is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != {"hooks"}:
        raise LedgerError("ledger must be a mapping with exactly one key, `hooks`")
    if not isinstance(doc["hooks"], list) or not doc["hooks"]:
        raise LedgerError("`hooks` must be a non-empty list")
    entries: dict[str, dict] = {}
    for i, entry in enumerate(doc["hooks"]):
        where = f"hooks[{i}]"
        if not isinstance(entry, dict) or set(entry) != {"id", "category", "evidence"}:
            raise LedgerError(f"{where}: keys must be exactly id, category, evidence")
        hook_id, category, evidence = entry["id"], entry["category"], entry["evidence"]
        if not _nonempty_str(hook_id):
            raise LedgerError(f"{where}: `id` must be a non-empty string")
        where = f"{where} ({hook_id})"
        if hook_id in entries:
            raise LedgerError(f"{where}: duplicate id")
        if category not in EVIDENCE_KEYS:
            raise LedgerError(
                f"{where}: category {category!r} is not one of {sorted(EVIDENCE_KEYS)}")
        if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS[category]:
            raise LedgerError(
                f"{where}: category {category} needs evidence keys exactly "
                f"{sorted(EVIDENCE_KEYS[category])}")
        if category == "b":
            ids = evidence["pytest"]
            if not isinstance(ids, list) or not ids or not all(map(_nonempty_str, ids)):
                raise LedgerError(f"{where}: `pytest` must be a non-empty list of node ids")
        elif not all(_nonempty_str(v) for v in evidence.values()):
            raise LedgerError(f"{where}: every evidence value must be a non-empty string")
        entries[hook_id] = entry
    return entries


def _repo_path(root: Path, rel: str) -> Path | None:
    """`root/rel` if `rel` is a plain relative path inside `root`, else None."""
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    return root / rel


def workflow_job_problem(root: Path, workflow: str, job: str) -> str | None:
    path = _repo_path(root, workflow)
    if path is None or not path.is_file():
        return f"workflow file {workflow!r} does not exist"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return f"workflow file {workflow!r} does not parse: {exc}"
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return f"workflow file {workflow!r} has no `jobs:` mapping"
    if job not in jobs:
        return f"workflow {workflow!r} has no job {job!r} (jobs: {sorted(jobs)})"
    return None


def node_id_problem(root: Path, node_id: str) -> str | None:
    """Why `node_id` does not resolve, or None. `[param]` suffixes are ignored."""
    file_part, *names = node_id.split("::")
    path = _repo_path(root, file_part)
    if path is None or path.suffix != ".py" or not path.is_file():
        return f"pytest file {file_part!r} does not exist"
    body: list[ast.stmt] = parse_py(path).body
    for depth, raw in enumerate(names):
        name = raw.split("[", 1)[0]
        found = next((n for n in body if isinstance(
            n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name), None)
        if found is None:
            scope = file_part if depth == 0 else "::".join([file_part, *names[:depth]])
            return f"{scope} has no {name!r}"
        body = found.body if isinstance(found, ast.ClassDef) else []
    return None


def ledger_violations(config: Mapping, workflows: Mapping[str, object],
                      ledger: Mapping[str, dict], root: Path) -> list[str]:
    """Every way the config / workflows / ledger disagree, each naming a hook."""
    auto = auto_stage_hooks(config)
    auto_set, every = set(auto), all_hooks(config)
    called = hooks_called_by_id(workflows)
    out = []
    for hook_id in auto:
        if hook_id not in called and hook_id not in ledger:
            out.append(
                f"{hook_id}: auto-stage hook has no CI execution point — call it "
                f"by id (`pre-commit run {hook_id} --all-files`) in a workflow, "
                f"or add it to {LEDGER.relative_to(REPO_ROOT).as_posix()}")
    for hook_id, entry in ledger.items():
        if hook_id not in every:
            out.append(f"{hook_id}: in the ledger but not in .pre-commit-config.yaml "
                       "— remove the entry")
            continue
        if hook_id not in auto_set:
            out.append(f"{hook_id}: in the ledger but not an auto-stage hook "
                       "— remove the entry")
            continue
        if hook_id in called:
            out.append(f"{hook_id}: in the ledger but already called by id in "
                       f"{sorted(set(called[hook_id]))} — remove the entry")
            continue
        evidence = entry["evidence"]
        if entry["category"] == "a":
            problem = workflow_job_problem(root, evidence["workflow"], evidence["job"])
            if problem:
                out.append(f"{hook_id}: (a) evidence does not resolve: {problem}")
        elif entry["category"] == "b":
            for node_id in evidence["pytest"]:
                problem = node_id_problem(root, node_id)
                if problem:
                    out.append(f"{hook_id}: (b) evidence {node_id!r} does not "
                               f"resolve: {problem}")
    return out


# ── the real repository ──────────────────────────────────────────────────────


def _real_workflows() -> dict[str, object]:
    files = [p for p in repo_files(".yml", ".yaml")
             if p.parent.relative_to(REPO_ROOT).as_posix() == WORKFLOW_DIR]
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8")) for p in files}


def test_every_auto_hook_has_a_declared_ci_execution_point() -> None:
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    workflows = _real_workflows()
    ledger = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    # Anti-vacuity: an empty side would make every loop above pass by
    # examining nothing.
    assert auto_stage_hooks(config), "no auto-stage hooks parsed"
    assert hooks_called_by_id(workflows), "no by-id `pre-commit run` found in any workflow"
    violations = ledger_violations(config, workflows, ledger, REPO_ROOT)
    assert not violations, "\n".join(
        ["pre-commit hooks vs CI execution points (#2694):", *violations])


# ── synthetic red / green table ──────────────────────────────────────────────


def _cfg(*hooks: dict, default_stages: list[str] | None = None) -> dict:
    cfg: dict = {"repos": [{"repo": "local", "hooks": list(hooks)}]}
    if default_stages is not None:
        cfg["default_stages"] = default_stages
    return cfg


def _wf(*runs: str, job: str = "lint") -> dict:
    return {"jobs": {job: {"steps": [{"run": r} for r in runs]}}}


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_probe.py").write_text(
        "class TestProbe:\n    def test_inner(self):\n        pass\n\n"
        "def test_top():\n    pass\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "probe.yml").write_text(
        yaml.safe_dump({"jobs": {"probe-job": {"steps": [{"run": "true"}]}}}),
        encoding="utf-8")
    return tmp_path


def _a(hook: str, job: str = "probe-job",
       workflow: str = ".github/workflows/probe.yml") -> dict:
    return {"id": hook, "category": "a",
            "evidence": {"workflow": workflow, "job": job, "note": "n"}}


def _b(hook: str, *node_ids: str) -> dict:
    return {"id": hook, "category": "b", "evidence": {"pytest": list(node_ids)}}


def _x(hook: str) -> dict:
    return {"id": hook, "category": "exempt", "evidence": {"reason": "r"}}


GREEN_LEDGER = [
    _a("by-job"),
    _b("by-test", "tests/test_probe.py", "tests/test_probe.py::test_top",
       "tests/test_probe.py::TestProbe::test_inner[param-1]"),
    _x("by-nothing"),
]
GREEN_HOOKS = [{"id": "called"}, {"id": "by-job"}, {"id": "by-test"},
               {"id": "by-nothing"}, {"id": "manual-only", "stages": ["manual"]}]
GREEN_WORKFLOWS = {"ci.yml": _wf("pre-commit run called --all-files",
                                 "# pre-commit run manual-only --all-files",
                                 "pre-commit run --all-files")}


def test_synthetic_green(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    ledger = parse_ledger(yaml.safe_dump({"hooks": GREEN_LEDGER}))
    assert ledger_violations(_cfg(*GREEN_HOOKS), GREEN_WORKFLOWS, ledger, root) == []


@pytest.mark.parametrize("hooks, workflows, entries, culprit, fragment", [
    pytest.param([*GREEN_HOOKS, {"id": "new-hook"}], GREEN_WORKFLOWS, GREEN_LEDGER,
                 "new-hook", "no CI execution point", id="unregistered-auto-hook"),
    pytest.param(GREEN_HOOKS, {"ci.yml": _wf("# pre-commit run called --all-files")},
                 GREEN_LEDGER, "called", "no CI execution point",
                 id="commented-call-does-not-count"),
    pytest.param(GREEN_HOOKS, {"ci.yml": _wf("true  # pre-commit run called")},
                 GREEN_LEDGER, "called", "no CI execution point",
                 id="trailing-comment-call-does-not-count"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS, [*GREEN_LEDGER, _x("gone")],
                 "gone", "not in .pre-commit-config.yaml", id="ledger-hook-removed"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS, [*GREEN_LEDGER, _x("manual-only")],
                 "manual-only", "not an auto-stage hook", id="ledger-hook-not-auto"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS, [*GREEN_LEDGER, _x("called")],
                 "called", "already called by id", id="ledger-hook-also-called"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS,
                 [_a("by-job", job="no-such-job"), *GREEN_LEDGER[1:]],
                 "by-job", "has no job 'no-such-job'", id="a-job-missing"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS,
                 [_a("by-job", workflow=".github/workflows/nope.yml"),
                  *GREEN_LEDGER[1:]],
                 "by-job", "does not exist", id="a-workflow-missing"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS,
                 [GREEN_LEDGER[0], _b("by-test", "tests/test_nope.py"), GREEN_LEDGER[2]],
                 "by-test", "does not exist", id="b-file-missing"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS,
                 [GREEN_LEDGER[0], _b("by-test", "tests/test_probe.py::test_nope"),
                  GREEN_LEDGER[2]],
                 "by-test", "has no 'test_nope'", id="b-function-missing"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS,
                 [GREEN_LEDGER[0],
                  _b("by-test", "tests/test_probe.py::TestProbe::test_nope"),
                  GREEN_LEDGER[2]],
                 "by-test", "TestProbe has no 'test_nope'", id="b-method-missing"),
    pytest.param(GREEN_HOOKS, GREEN_WORKFLOWS,
                 [GREEN_LEDGER[0], _b("by-test", "tests/test_probe.py::test_inner"),
                  GREEN_LEDGER[2]],
                 "by-test", "has no 'test_inner'", id="b-method-is-not-top-level"),
])
def test_synthetic_red(tmp_path: Path, hooks, workflows, entries, culprit,
                       fragment) -> None:
    root = _tree(tmp_path)
    ledger = parse_ledger(yaml.safe_dump({"hooks": entries}))
    violations = ledger_violations(_cfg(*hooks), workflows, ledger, root)
    named = [v for v in violations if v.startswith(f"{culprit}:")]
    assert named and any(fragment in v for v in named), violations


@pytest.mark.parametrize("hook, default_stages, auto", [
    pytest.param({"id": "h"}, None, True, id="no-stages-anywhere-is-every-stage"),
    pytest.param({"id": "h"}, ["pre-commit"], True, id="default-pre-commit"),
    pytest.param({"id": "h"}, ["manual"], False, id="default-manual-silences"),
    pytest.param({"id": "h", "stages": ["pre-commit"]}, ["manual"], True,
                 id="hook-stages-override-default"),
    pytest.param({"id": "h", "stages": ["commit"]}, None, True, id="legacy-commit-alias"),
    pytest.param({"id": "h", "stages": ["pre-push"]}, None, False, id="pre-push-only"),
])
def test_auto_stage_resolution(hook: dict, default_stages, auto: bool) -> None:
    assert (auto_stage_hooks(_cfg(hook, default_stages=default_stages)) == ["h"]) is auto


@pytest.mark.parametrize("text", [
    pytest.param("hooks: [\n", id="invalid-yaml"),
    pytest.param("", id="empty-file"),
    pytest.param("- id: x\n", id="top-level-list"),
    pytest.param("hooks: []\n", id="no-entries"),
    pytest.param("hooks: []\nextra: 1\n", id="extra-top-key"),
    pytest.param(yaml.safe_dump({"hooks": [{"id": "x", "category": "c",
                                           "evidence": {"reason": "r"}}]}),
                 id="unknown-category"),
    pytest.param(yaml.safe_dump({"hooks": [{"id": "x", "category": "a",
                                           "evidence": {"workflow": "w"}}]}),
                 id="a-missing-job"),
    pytest.param(yaml.safe_dump({"hooks": [{"id": "x", "category": "b",
                                           "evidence": {"pytest": []}}]}),
                 id="b-empty-node-ids"),
    pytest.param(yaml.safe_dump({"hooks": [{"id": "x", "category": "exempt",
                                           "evidence": {"reason": "  "}}]}),
                 id="exempt-blank-reason"),
    pytest.param(yaml.safe_dump({"hooks": [{"id": "x", "category": "exempt"}]}),
                 id="missing-evidence"),
    pytest.param(yaml.safe_dump({"hooks": [_x("x"), _x("x")]}), id="duplicate-id"),
])
def test_malformed_ledger_fails_closed(text: str) -> None:
    with pytest.raises(LedgerError):
        parse_ledger(text)


def test_non_mapping_workflow_fails_closed() -> None:
    with pytest.raises(LedgerError):
        hooks_called_by_id({"broken.yml": ["not", "a", "workflow"]})
