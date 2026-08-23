"""
Golden parity test for describe_tenant.py deep_merge + inheritance.

This test is the trump card for ADR-017 semantic verification:
- Runs describe_tenant.py against every scenario captured in golden.json
- Compares source_hash + merged_hash + effective_config against golden.json
- If any hash diverges, either:
    (a) Python describe_tenant.py logic changed → bug or intentional update (regen golden)
    (b) A Go port produces different hashes → semantic drift, fix the Go side

Fixtures cover every deep_merge rule from ADR-017:
- flat:              no defaults chain
- l0-only:           root _defaults + tenant override (scalar)
- full-l0-l3:        4-level inheritance, array replace, tenant override
- mixed-mode:        flat + hierarchical tenants in same conf.d
- array-replace:     arrays replaced (not concat)
- opt-out-null:      null deletes a reserved key, not a threshold key
- opt-out-null-threshold: real flat-metric-key shape — null keeps the
                     inherited default, "disable" is the opt-out (#1339)
- metadata-skipped:  _metadata never propagates
- wrapper-siblings:  `defaults:` wrapper WITH sibling top-level keys — the
                     shape the shipped platform file has. See
                     build_and_capture.py for what it does and does not buy.

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
    # encoding='utf-8' so Windows hosts (where Python's default text-mode
    # decoder is the system codepage, e.g. cp950) decode the child Python's
    # UTF-8 output correctly. Pairs with the session-scoped
    # PYTHONIOENCODING=utf-8 fixture in tests/conftest.py.
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"describe_tenant failed for {tenant_id}: {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="describe_tenant emits OS-native path separators for "
           "defaults_chain entries (`db\\_defaults.yaml` on Windows vs "
           "`db/_defaults.yaml` in the golden). The defaults_chain "
           "comparison only matches on POSIX. Fixing describe_tenant "
           "to normalise to forward-slash is a separate behavior change.",
)
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
# Go parity lives in the Go tree, not here.
# -------------------------------------------------------------------------
#
# A `test_merge_parity_go` used to sit here, shelling out to
# `components/threshold-exporter/bin/threshold-exporter dump-merged`.
# Removed: nothing in the repo builds that path (its own docstring was the
# only mention of it), no `dump-merged` subcommand exists in the Go
# sources, and measured in the Dev Container — which does have Go — the
# whole parametrized set reported `skipped`. It asserted nothing while
# calling itself "the single most important test for ADR-017 conformance".
#
# The Go end of the parity contract is
# components/threshold-exporter/app/config_golden_parity_test.go, which
# reads this same golden.json directly and runs under `Go Tests`.
