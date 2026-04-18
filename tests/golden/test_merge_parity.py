"""
Golden parity test for describe_tenant.py deep_merge + inheritance.

This test is the trump card for ADR-018 semantic verification:
- Runs describe_tenant.py against 8 deterministic fixture scenarios
- Compares source_hash + merged_hash + effective_config against golden.json
- If any hash diverges, either:
    (a) Python describe_tenant.py logic changed → bug or intentional update (regen golden)
    (b) A Go port produces different hashes → semantic drift, fix the Go side

Fixtures cover every deep_merge rule from ADR-018:
- flat:              no defaults chain
- l0-only:           root _defaults + tenant override (scalar)
- full-l0-l3:        4-level inheritance, array replace, tenant override
- mixed-mode:        flat + hierarchical tenants in same conf.d
- array-replace:     arrays replaced (not concat)
- opt-out-null:      explicit null deletes inherited key
- metadata-skipped:  _metadata never propagates

Regenerate golden.json by running tests/golden/build_and_capture.py after
intentional semantic changes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
GOLDEN = json.loads((HERE / "golden.json").read_text(encoding="utf-8"))


def _fixture_path(fixture_dir: str) -> Path:
    return HERE / "fixtures" / fixture_dir / "conf.d"


def _run_describe(conf_d: Path, tenant_id: str) -> dict:
    """Invoke describe_tenant.py as a subprocess; return parsed JSON output."""
    cmd = [
        sys.executable,
        str(DESCRIBE),
        tenant_id,
        "--conf-d", str(conf_d),
        "--show-sources",
        "--format", "json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.fail(f"describe_tenant failed for {tenant_id}: {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.parametrize("golden", GOLDEN, ids=lambda g: f"{g['scenario']}/{g['tenant_id']}")
def test_merge_parity_python(golden: dict):
    """Verify current describe_tenant output matches captured golden hashes.

    This guards against:
    - Accidental changes to deep_merge semantics
    - Changes to canonical JSON representation (separator/sort/encoding)
    - Changes to SHA-256 truncation (currently 16 hex chars)
    """
    conf_d = _fixture_path(golden["fixture_dir"])
    assert conf_d.exists(), f"Fixture dir missing: {conf_d}"

    result = _run_describe(conf_d, golden["tenant_id"])

    assert result["source_hash"] == golden["source_hash"], \
        f"source_hash drift for {golden['scenario']}"
    assert result["merged_hash"] == golden["merged_hash"], \
        f"merged_hash drift for {golden['scenario']}"
    assert result["effective_config"] == golden["effective_config"], \
        f"effective_config drift for {golden['scenario']}"
    assert result["defaults_chain"] == golden["defaults_chain"], \
        f"defaults_chain order drift for {golden['scenario']}"


# -------------------------------------------------------------------------
# Go parity — skipped unless Go binary is available (Dev Container only)
# -------------------------------------------------------------------------

def _go_binary_path() -> Path | None:
    """Locate the threshold-exporter Go binary for parity verification.

    Dev Container builds to: components/threshold-exporter/bin/threshold-exporter
    Returns None if binary doesn't exist (Cowork VM doesn't have Go).
    """
    candidate = REPO_ROOT / "components" / "threshold-exporter" / "bin" / "threshold-exporter"
    return candidate if candidate.exists() else None


@pytest.mark.skipif(_go_binary_path() is None, reason="Go binary not built (skipped outside Dev Container)")
@pytest.mark.parametrize("golden", GOLDEN, ids=lambda g: f"{g['scenario']}/{g['tenant_id']}")
def test_merge_parity_go(golden: dict):
    """Verify Go port of deep_merge produces byte-identical hashes to Python.

    Expects the Go binary to expose a subcommand like:
        threshold-exporter dump-merged --conf-d <path> --tenant <id>
    emitting JSON: {"source_hash": "...", "merged_hash": "...", "effective_config": {...}}

    This is the single most important test for ADR-018 conformance.
    Any divergence = immediate blocker on v2.7.0 tag.
    """
    binary = _go_binary_path()
    conf_d = _fixture_path(golden["fixture_dir"])

    cmd = [
        str(binary), "dump-merged",
        "--conf-d", str(conf_d),
        "--tenant", golden["tenant_id"],
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.fail(f"Go dump-merged failed for {golden['tenant_id']}: {result.stderr}")

    go_result = json.loads(result.stdout)

    assert go_result["source_hash"] == golden["source_hash"], \
        f"Go source_hash != Python for {golden['scenario']}"
    assert go_result["merged_hash"] == golden["merged_hash"], \
        f"Go merged_hash != Python for {golden['scenario']} — ADR-018 semantic drift"
    # effective_config parity is stricter — catches ordering / type-coercion drift
    assert go_result["effective_config"] == golden["effective_config"], \
        f"Go effective_config != Python for {golden['scenario']}"
