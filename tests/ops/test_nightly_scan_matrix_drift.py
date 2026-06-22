"""Drift guard for the nightly third-party scan matrix (#902 L1-A drift guard).

Closes the dual-SSOT gap raised in #907 review: the `scan-thirdparty` matrix in
nightly-image-scan.yaml hardcodes the 14 third-party refs, while the actual
deployment refs live in helm values / k8s manifests. If a maintainer bumps a
manifest (e.g. grafana 12.4.2 -> 12.5.0) but forgets the scan matrix, the scan
would keep reporting the OLD version as "safe" while prod runs the new one —
false security ("scanning a parallel universe").

This guard makes the L1-B extractor (which reads the real values/manifests) the
single source of truth and fails CI on drift:
  * scan-thirdparty matrix refs MUST equal `check_image_refs_resolve.py --list`
  * the report's EXPECTED counts MUST equal the matrix sizes (so the "X/Y images"
    + degraded-scan logic stays correct).

Network-free (uses --list), so it runs in the plain Python Tests CI job.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "nightly-image-scan.yaml"
EXTRACTOR = ROOT / "scripts" / "ops" / "check_image_refs_resolve.py"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _matrix_include(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["strategy"]["matrix"]["include"]


def _aggregate_run() -> str:
    steps = _workflow()["jobs"]["report"]["steps"]
    agg = next(s for s in steps if "Aggregate" in (s.get("name") or ""))
    return agg["run"]


def test_thirdparty_matrix_equals_deployed_refs() -> None:
    """scan-thirdparty matrix == the refs the extractor finds in values/manifests."""
    matrix_refs = {e["ref"] for e in _matrix_include("scan-thirdparty")}

    proc = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--root", str(ROOT), "--list"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    deployed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    assert matrix_refs == deployed, (
        "scan-thirdparty matrix drifted from the deployed third-party image set.\n"
        f"  only in scan matrix : {sorted(matrix_refs - deployed)}\n"
        f"  only in deployed    : {sorted(deployed - matrix_refs)}\n"
        "Sync the scan-thirdparty matrix in .github/workflows/nightly-image-scan.yaml "
        "with the chart values / k8s manifests (or adjust the extractor skip-lists)."
    )


def test_report_expected_counts_match_matrix_sizes() -> None:
    """The report's hardcoded EXPECTED (5 / 14) must track the matrix sizes."""
    n_selfbuilt = len(_matrix_include("scan"))
    n_thirdparty = len(_matrix_include("scan-thirdparty"))
    run = _aggregate_run()

    m_sb = re.search(r'frags-sb.*?\s(\d+)\s+"self-built component"', run, re.S)
    m_tp = re.search(r'frags-tp.*?\s(\d+)\s+"third-party upstream image"', run, re.S)

    assert m_sb is not None, "could not find the self-built file_cve_report.sh EXPECTED arg"
    assert m_tp is not None, "could not find the third-party file_cve_report.sh EXPECTED arg"
    assert int(m_sb.group(1)) == n_selfbuilt, (
        f"report self-built EXPECTED={m_sb.group(1)} != {n_selfbuilt} scan matrix entries"
    )
    assert int(m_tp.group(1)) == n_thirdparty, (
        f"report third-party EXPECTED={m_tp.group(1)} != {n_thirdparty} scan-thirdparty entries"
    )
