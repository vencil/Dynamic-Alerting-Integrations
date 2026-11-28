"""Where generator-service's writes land, and through what (#3981).

The owner's third step for the runtime class (2026-11-28): run the service
under the write audit (``tests/shared/_write_audit/sitecustomize.py``) on a
control — its start, the first self-check round, ``/v1/status`` with a
``--policy`` and one judgement whose write goes into a subdirectory — and
hold every filesystem change the log shows:

* under ``--work-dir``: made by the runtime entry, by the functions that DO
  its writes (``_make_private_dir`` / ``_create_private_file``, attributed
  as the write audit of ``test_output_path_write_failure`` attributes the
  operator and artifact classes). A raw write there, or one through another
  entry, is a fault.
* a runtime-entry write anywhere else is a fault too — but the parents a
  start makes for a ``--work-dir`` that is not there.
* every function that calls a runtime entry (read off the two modules by
  ``test_runtime_entry_callers.calling_functions``) is reached by the run:
  a control that skipped one would not have measured its writes.

What is not counted, each for a stated reason:

* deletions (``os.remove`` / ``os.rmdir``): the cleanup of a job or a
  self-check directory, and a write that deletes a file. A removal writes
  nothing, as in ``test_raw_write_sinks_are_listed``.
* ONE operator write: ``findings.json`` in a job directory, written by the
  generator itself — ``generate_alertmanager_routes`` run as its CLI by
  the judgement (``_gsvc_child._judge``), through the operator entry, at
  the path the judgement names on its ``--findings-json``. The generator's
  CLI contract writes that flag's file through the operator entry; held
  here to exactly that file, from exactly those frames.

WHAT IT DOES NOT SEE
--------------------
A program that is not Python (``git``, ``da-guard``, ``da-read``) writes
where the hook cannot see: a self-check's ``da-guard`` makes its fixture
tree in its directory under ``--work-dir`` (its ``$TMPDIR``). The spawns
are in the log; what they write is not.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import _grar_findings_cases as cases  # noqa: E402
import _gsvc_harness as h  # noqa: E402
import test_output_path_write_failure as audit  # noqa: E402  (the write audit's reader)
import test_runtime_entry_callers as callers  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the service is Linux-only")

_GENERATOR = "scripts/tools/ops/generate_alertmanager_routes.py:"
_JUDGE = "scripts/tools/ops/_gsvc_child.py:_judge"
_ENTRY_FILE = "scripts/tools/_lib_output.py:"
_DELETIONS = ("os.remove", "os.rmdir")


def _is_the_generators_findings(event: dict, placed: str, work: Path) -> bool:
    """The one operator write under --work-dir (module docstring): the
    generator's ``--findings-json`` file in a job directory, from the
    judgement's run of it."""
    stack = event.get("stack") or []
    rel = audit._under(placed, work)
    parts = rel.split("/") if rel else []
    return (len(parts) == 3 and parts[0] == "jobs" and parts[1].startswith("job-")
            and parts[2] == "findings.json"
            and _JUDGE in stack and any(f.startswith(_GENERATOR) for f in stack))


def runtime_faults(events: list[dict] | None, work: Path, tool: Path) -> list[str]:
    """Every fault of one run's log against the rules of the module
    docstring. Pure: the controls below drive it with hand-made logs."""
    if events is None:
        return ["the write audit left no log: nothing was measured"]
    frames = audit._entry_frames()
    root = os.path.realpath(work)
    faults: list[str] = []
    kept: list[dict] = []
    for event in events:
        if event["ev"] in ("loaded", "spawn", "read", "hook-error") or event.get("unplaced"):
            kept.append(event)  # `_audit` judges these (an unplaced one is a fault)
            continue
        placed = audit._placed(event)
        if placed is None:
            continue  # a descriptor that is no file: nowhere a write can land
        kind = audit._via(event, frames)
        under = audit._under(placed, work) is not None
        if kind == "runtime" and not under:
            parent_of_it = event["ev"] == "os.mkdir" and root.startswith(placed + os.sep)
            if not parent_of_it:
                faults.append(f"{event['ev']} {placed} through the runtime entry, outside "
                              f"--work-dir {root}")
            continue
        if not under:
            continue
        if event["ev"] in _DELETIONS and kind != "runtime":
            continue
        if kind == "operator" and _is_the_generators_findings(event, placed, work):
            continue
        kept.append(event)
    seen = audit._audit(kept, work, tool)
    if not seen.loaded:
        faults.append("no `loaded` line from the service's own process: its writes "
                      "were not measured")
    faults.extend(seen.faults)
    for kind, sites in sorted(seen.sites.items()):
        if kind != "runtime":
            faults.append(f"{kind} write(s) under --work-dir, not through the runtime "
                          f"entry: {list(sites)}")
    if not seen.sites.get("runtime"):
        faults.append("no write through the runtime entry at all: a dead control")
    return faults


def reached_callers(events: list[dict]) -> frozenset[str]:
    """``<file>:<function>`` of the frame just outside the runtime entry,
    for every event the entry raised."""
    frames = audit._entry_frames()
    found = set()
    for event in events:
        stack = event.get("stack") or []
        if event["ev"] in ("loaded", "spawn") or audit._via(event, frames) != "runtime":
            continue
        outside = next((f for f in stack if not f.startswith(_ENTRY_FILE)), None)
        if outside is not None:
            found.add(outside)
    return frozenset(found)


def expected_callers() -> frozenset[str]:
    """Every function that calls a runtime entry in the modules that may
    (`test_runtime_entry_callers`), as a stack frame names it."""
    out = set()
    entries = callers.runtime_entries()
    for rel in callers.callers():
        tree = callers.ast.parse((h.REPO / rel).read_text(encoding="utf-8"), rel)
        out |= {f"{rel}:{fn}" for fn in callers.calling_functions(tree, entries)}
    return frozenset(out)


@pytest.fixture(scope="module")
def control(tmp_path_factory):
    """One run of the service under the write audit: start, the first
    self-check round, ``/v1/status`` with a policy, one judgement whose
    base tree and write both have a subdirectory — and one on a base with
    no conf.d at all, whose writes make the tree (`apply_writes` is then
    the first to make its directory)."""
    tmp = tmp_path_factory.mktemp("gsvc-rt")
    repo = h.init_repo(tmp / "repo")
    h.run_git(repo, "commit", "-q", "--allow-empty", "-m", "nothing yet")
    empty = h.run_git(repo, "rev-parse", "HEAD")
    files = dict(cases.FIXTURES["clean"])
    files["team/beta.yaml"] = files.pop("beta.yaml")
    commit = h.commit_tree(repo, files)
    policy = tmp / "policy.yaml"
    policy.write_text("allowed_domains: []\n", encoding="utf-8")
    log = tmp / "audit.log"
    env = audit._audit_env(log, tmp)  # TMPDIR beside, never under, --work-dir
    svc = h.Service(tmp / "s", repo, bin_dir=tmp / "bin", env=env,
                    args=("--policy", str(policy)))
    try:
        status, st = svc.call("GET", "/v1/status")
        assert status == 200 and st["ready"] is True, st
        status, doc = svc.validate(commit, [{"path": "team/gamma.yaml",
                                             "content_b64": h.b64(cases._tenant("gamma"))}])
        assert status == 200 and doc["result"] == "pass", doc
        status, doc = svc.validate(empty, [{"path": rel, "content_b64": h.b64(body)}
                                           for rel, body in sorted(files.items())])
        assert status == 200 and doc["result"] == "pass", doc
    finally:
        svc.stop()
    return {"events": audit._read_audit_log(log), "work": svc.work,
            "tool": svc.tmp / "launcher.py"}


def test_every_write_under_the_work_dir_comes_from_the_runtime_entry(control):
    faults = runtime_faults(control["events"], control["work"], control["tool"])
    assert not faults, "\n  ".join(["generator-service's writes:", *faults])


def test_the_control_reaches_every_caller_of_the_runtime_entry(control):
    want = expected_callers()
    assert want, "no caller of a runtime entry was read: the comparison holds nothing"
    missing = sorted(want - reached_callers(control["events"]))
    assert not missing, ("the control run never wrote through these callers of the "
                         f"runtime entry, so their writes were not measured: {missing}")


# ── Controls: the verdict on hand-made logs ────────────────────────────────

_SVC = "scripts/tools/ops/generator_service.py:"
_RT_FILE = [f"{_ENTRY_FILE}_create_private_file", f"{_ENTRY_FILE}open_runtime_file",
            f"{_ENTRY_FILE}write_runtime_file", f"{_SVC}snapshot_policy"]
_RT_DIR = [f"{_ENTRY_FILE}_make_private_dir", f"{_ENTRY_FILE}make_runtime_dir",
           f"{_SVC}_new_job_dir"]
_OP = [f"{_ENTRY_FILE}_kernel_open_operator_output", f"{_ENTRY_FILE}open_operator_output",
       f"{_ENTRY_FILE}write_operator_output"]


class _Log:
    def __init__(self, tmp: Path):
        self.work = tmp / "work"
        self.tool = tmp / "launcher.py"
        self.events: list[dict] = [{"ev": "loaded", "argv": ["python3", "-s", str(self.tool)]}]

    def add(self, ev: str, path: str, stack: list[str], **more) -> "_Log":
        self.events.append({"ev": ev, "path": path if os.path.isabs(path)
                            else str(self.work / path), "stack": stack, **more})
        return self

    def faults(self) -> list[str]:
        return runtime_faults(self.events, self.work, self.tool)


def _clean(tmp: Path) -> _Log:
    log = _Log(tmp).add("os.mkdir", "jobs/job-1", _RT_DIR)
    return log.add("open", "jobs/job-1/policy.yaml", _RT_FILE)


def test_a_run_through_the_runtime_entry_is_clean(tmp_path):
    log = _clean(tmp_path).add("os.chmod", "jobs/job-1", _RT_DIR, fd=4)
    log.add("open", "jobs/job-1/policy.yaml", _RT_FILE[2:], fd=5)
    log.add("os.mkdir", str(tmp_path), _RT_DIR)  # a parent of --work-dir, made at start
    assert log.faults() == []


def test_a_raw_write_under_the_work_dir_is_a_fault(tmp_path):
    faults = _clean(tmp_path).add("open", "jobs/job-1/blob-ids", [f"{_SVC}snapshot_policy"]).faults()
    assert len(faults) == 1 and "raw write(s) under --work-dir" in faults[0], faults


def test_a_write_naming_the_entry_but_not_made_by_it_is_a_fault(tmp_path):
    faults = _clean(tmp_path).add("open", "jobs/job-1/x", _RT_FILE[1:]).faults()
    assert len(faults) == 1 and "not by `_make_private_dir`" in faults[0], faults


def test_a_runtime_write_outside_the_work_dir_is_a_fault(tmp_path):
    log = _clean(tmp_path).add("open", str(tmp_path / "elsewhere"), _RT_FILE)
    faults = log.faults()
    assert len(faults) == 1 and "outside --work-dir" in faults[0], faults
    # a mkdir of a directory that is no parent of --work-dir is not excused
    beside = _clean(tmp_path).add("os.mkdir", str(tmp_path / "work-2"), _RT_DIR).faults()
    assert len(beside) == 1 and "outside --work-dir" in beside[0], beside


def test_the_generators_findings_are_the_one_operator_write(tmp_path):
    gen = [*_OP, f"{_GENERATOR}main", _JUDGE, "scripts/tools/ops/_gsvc_child.py:run_child"]
    assert _clean(tmp_path).add("open", "jobs/job-1/findings.json", gen).faults() == []
    # the same file from another caller, another file from the generator
    other = _clean(tmp_path).add("open", "jobs/job-1/findings.json", [*_OP, f"{_SVC}_judge"])
    assert any("operator write(s) under --work-dir" in f for f in other.faults())
    out = _clean(tmp_path).add("open", "jobs/job-1/routes.yaml", gen)
    assert any("operator write(s) under --work-dir" in f for f in out.faults())
    top = _clean(tmp_path).add("open", "findings.json", gen)
    assert any("operator write(s) under --work-dir" in f for f in top.faults())


def test_a_deletion_is_not_a_write_and_an_unplaced_event_is_a_fault(tmp_path):
    log = _clean(tmp_path).add("os.remove", "jobs/job-1/policy.yaml", [f"{_SVC}_judge"])
    log.add("os.rmdir", "jobs/job-1", [f"{_SVC}_judge"])
    assert log.faults() == []
    unplaced = _clean(tmp_path)
    unplaced.events.append({"ev": "open", "path": "x", "unplaced": True, "stack": _RT_FILE})
    assert any("could not be placed" in f for f in unplaced.faults())


def test_no_log_no_service_line_and_no_runtime_write_are_each_a_fault(tmp_path):
    assert runtime_faults(None, tmp_path / "work", tmp_path / "launcher.py") != []
    silent = _clean(tmp_path)
    silent.events.pop(0)
    assert any("no `loaded` line" in f for f in silent.faults())
    assert any("dead control" in f for f in _Log(tmp_path).faults())


def test_reaching_is_read_off_the_frame_just_outside_the_entry(tmp_path):
    log = _clean(tmp_path).add("open", "jobs/job-1/findings.json",
                               [*_OP, f"{_GENERATOR}main", _JUDGE])
    assert reached_callers(log.events) == {f"{_SVC}_new_job_dir", f"{_SVC}snapshot_policy"}
