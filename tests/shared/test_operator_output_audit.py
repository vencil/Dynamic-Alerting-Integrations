"""#3107 v2 structural gate (prototype): an operator-output path is written
only by ``_lib_io``'s operator writer, and never renamed onto.

Each row runs the real CLI with the flag pointing at a sentinel path, under
a start-up audit hook (``_write_audit/sitecustomize.py``) that logs every
write-mode open, rename and chmod. Blind spot: a write path no row exercises.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AUDIT_DIR = Path(__file__).resolve().parent / "_write_audit"
CONF = REPO / "components/threshold-exporter/config/conf.d"
OPERATOR_WRITER = "_lib_io.py:_kernel_open_operator_output"
ARTIFACT_WRITER = "_lib_atomic_write.py:"

# Operator outputs: opened by the kernel through `_lib_io`'s operator writer,
# never renamed onto.
ROWS = {
    "gar --findings-json": lambda p, d: ["scripts/tools/ops/generate_alertmanager_routes.py",
                                         "--config-dir", str(CONF), "-o", str(d / "r.yaml"),
                                         "--findings-json", str(p)],
    "fed-key --jwks-out": lambda p, d: ["scripts/tools/ops/federation_keygen.py", "--jwks-out", str(p)],
    "assemble --manifest": lambda p, d: ["scripts/tools/ops/assemble_config_dir.py", "--sources", str(CONF),
                                         "--output", str(d / "out"), "--manifest", str(p)],
}

# Artifacts (#3107 risk 1, owner 2026-10-24: cmasm is one): replaced
# atomically by `_lib_atomic_write` — a tmp beside the target, renamed onto
# it — and by nothing else.
ARTIFACT_ROWS = {
    "configmap_assemble --output": lambda p, d: ["scripts/ops/configmap_assemble.py",
                                                 "--config-dir", str(CONF), "--output", str(p)],
    "state-reconcile --manifest-path": lambda p, d: ["scripts/tools/ops/state_reconcile.py",
                                                     "--state-dir", str(_state_dir(d)), "--manifest-path", str(p)],
}


def _state_dir(d: Path) -> Path:
    s = d / "state"
    s.mkdir(exist_ok=True)
    return s


def _kubectl_shim(tmp: Path) -> Path:
    """The ConfigMap-building kubectl stand-in of test_configmap_assemble."""
    spec = importlib.util.spec_from_file_location(
        "_cmasm_tests", REPO / "tests" / "ops" / "test_configmap_assemble.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp / "kubectl-shim"
    d.mkdir()
    exe = d / "kubectl"
    exe.write_text(mod._SHIM.format(python=sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    return d


def _run_audited(argv: list[str], tmp: Path) -> list[dict]:
    log = tmp / "audit.jsonl"
    env = dict(os.environ, DA_WRITE_AUDIT_LOG=str(log), ALLOW_SAMPLE_CONFDIR="1",
               PYTHONPATH=os.pathsep.join([str(AUDIT_DIR), os.environ.get("PYTHONPATH", "")]),
               PATH=os.pathsep.join([str(_kubectl_shim(tmp)), os.environ.get("PATH", "")]))
    subprocess.run([sys.executable, *argv], cwd=REPO, env=env, capture_output=True,
                   text=True, encoding="utf-8", timeout=120, check=False)
    if not log.exists():
        pytest.fail("audit hook did not load (no log) — the gate measured nothing")
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _touching(events: list[dict], target: Path) -> list[dict]:
    """Events on *target* or on a file beside it named after it (a tmp)."""
    return [e for e in events if os.path.abspath(e["path"]) == str(target)
            or (os.path.dirname(os.path.abspath(e["path"])) == str(target.parent)
                and os.path.basename(e["path"]).startswith(target.name))]


@pytest.mark.parametrize("row", sorted(ROWS))
def test_operator_output_written_only_by_the_operator_writer(row: str, tmp_path: Path) -> None:
    target = tmp_path / "target.out"
    mine = _touching(_run_audited(ROWS[row](target, tmp_path), tmp_path), target)
    assert any(e["ev"] == "open" for e in mine), f"{row}: no write reached {target} — row is dead"
    bad = [e for e in mine if e["ev"] != "open" or OPERATOR_WRITER not in e["stack"]]
    assert not bad, f"{row}: writes to the operator path outside _lib_io: {bad}"


@pytest.mark.parametrize("row", sorted(ARTIFACT_ROWS))
def test_artifact_written_only_by_the_atomic_writer(row: str, tmp_path: Path) -> None:
    target = tmp_path / "target.out"
    mine = _touching(_run_audited(ARTIFACT_ROWS[row](target, tmp_path), tmp_path), target)
    renamed = [e for e in mine if e["ev"] == "rename" and os.path.abspath(e["path"]) == str(target)]
    assert renamed, f"{row}: nothing was renamed onto {target} — not atomic, or the row is dead: {mine}"
    bad = [e for e in mine if not any(f.startswith(ARTIFACT_WRITER) for f in e["stack"])]
    assert not bad, f"{row}: writes to the artifact outside _lib_atomic_write: {bad}"
    direct = [e for e in mine if e["ev"] == "open" and os.path.abspath(e["path"]) == str(target)]
    assert not direct, f"{row}: the artifact itself was opened for writing (not atomic): {direct}"
