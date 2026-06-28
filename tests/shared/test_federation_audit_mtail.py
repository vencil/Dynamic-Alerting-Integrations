"""test_federation_audit_mtail.py — federation-audit.mtail compile gate (#908)

Codifies a CI compile check for
``helm/federation-gateway/files/federation-audit.mtail`` — the mtail program that
derives ``tenant_federation_requests_total`` (ADR-020 IV-2f) and
``tenant_log_query_requests_total{account_id,project_id,status}`` /
``tenant_log_query_duration_ms`` (ADR-021 #609 PR-4) from the Envoy audit access
log.

Why this gate exists (#908 secondary line): the ``.mtail`` program is COMPILED by
the mtail sidecar at POD START. A syntax / type regression in the program
therefore surfaces only at deploy time (the sidecar CrashLoops), NOT in CI —
mtail otherwise has zero CI presence. This pins ``mtail --compile_only`` as a
gate, mirroring the ``vector validate`` codification in
``test_vector_projection_vrl.py`` and the promtool rule-pack gate.

mtail 3.0.8 — the SAME version the audit-sidecar deploys
(``helm/federation-gateway/audit-sidecar/Dockerfile`` ``MTAIL_VERSION`` /
``values.yaml`` ``auditLog.image.tag``) — so the gate compiles against exactly
the runtime compiler.

Gated on ``mtail`` being on PATH (``shutil.which``); CI installs a pinned +
checksum-verified mtail (ci.yml ``python-tests`` job) so the gate is REAL there.
Without that install the behavioural tests SILENTLY SKIP — the same philosophy as
the Vector / Helm installs alongside them.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HAS_MTAIL = shutil.which("mtail") is not None
_needs_mtail = pytest.mark.skipif(not _HAS_MTAIL, reason="mtail CLI not on PATH")

# Path (repo-relative) of the program the chart's configmap-mtail.yaml renders via
# .Files.Get — keep in lockstep if the program is ever moved/renamed.
_MTAIL_PROG = "helm/federation-gateway/files/federation-audit.mtail"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent.parent


@pytest.fixture(scope="session")
def mtail_prog(repo_root: Path) -> Path:
    return repo_root / _MTAIL_PROG


def _compile(progdir: Path) -> subprocess.CompletedProcess:
    """Run ``mtail --compile_only`` over a directory of ``.mtail`` programs.

    ``--compile_only`` compiles and exits without loading the VM, so no
    ``--logs`` / ``--port`` is needed — it is the pre-deploy equivalent of the
    validation the sidecar implicitly does at startup. ``--logtostderr`` routes
    glog (and thus any compile diagnostic) to stderr where we capture it.
    """
    return subprocess.run(
        ["mtail", "--compile_only", "--logtostderr", "--progs", str(progdir)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_mtail_program_exists(mtail_prog: Path) -> None:
    """Non-gated guard: the program is at the path configmap-mtail.yaml's
    ``.Files.Get`` expects. Catches a rename/move even where mtail is not
    installed (so the gate's premise never silently rots)."""
    assert mtail_prog.is_file(), f"mtail program missing at {_MTAIL_PROG}"


@_needs_mtail
def test_federation_audit_mtail_compiles(mtail_prog: Path, tmp_path: Path) -> None:
    """THE GATE: the shipped federation-audit.mtail compiles clean under the
    deployed mtail 3.0.8. A syntax / type regression fails CI here instead of
    CrashLooping the sidecar at deploy.

    The program is copied into an isolated dir so ``--progs`` sees ONLY it (the
    chart's files/ dir also holds envoy.yaml + .lua, which mtail would ignore,
    but an isolated dir keeps the gate hermetic)."""
    progdir = tmp_path / "progs"
    progdir.mkdir()
    (progdir / mtail_prog.name).write_text(
        mtail_prog.read_text(encoding="utf-8"), encoding="utf-8"
    )

    res = _compile(progdir)
    assert res.returncode == 0, (
        f"mtail --compile_only FAILED for {_MTAIL_PROG}:\n"
        f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
    )


@_needs_mtail
def test_compile_gate_rejects_broken_program(tmp_path: Path) -> None:
    """Positive control: prove the gate actually catches errors (it is not a
    no-op that always passes). A program with an unterminated block MUST fail to
    compile — if this ever passes, the gate above is meaningless."""
    progdir = tmp_path / "progs"
    progdir.mkdir()
    # Valid-looking but truncated: the counter is declared, the match block is
    # opened and never closed → a guaranteed parse error in any mtail version.
    (progdir / "broken.mtail").write_text(
        "counter requests_total\n/pattern/ {\n  requests_total++\n",
        encoding="utf-8",
    )

    res = _compile(progdir)
    assert res.returncode != 0, (
        "mtail --compile_only PASSED a deliberately broken program — the compile "
        f"gate is a no-op.\n--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
    )
