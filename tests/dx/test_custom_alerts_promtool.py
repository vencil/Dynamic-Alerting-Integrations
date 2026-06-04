"""promtool behavioral golden tests for the Custom Alerts compiler (#741 S1+S2).

S1+S2 delivers the COMPILER, not a committed/deployed rule pack — the exporter
does not yet emit `user_threshold{recipe_id,name}` (that is S3), and the repo's
#731 closed-label contract (rulepack_contract_test.go) rightly refuses a pack
that references not-yet-emittable labels. So these promtool goldens live OUTSIDE
tests/rulepacks/ (which the #731 fixture contract scans) and are driven HERE:
compile the example fixture → a temp pack → run `promtool test rules` against it.

Coverage: fire / no-version main path / version fallback / maintenance suppress
/ selector filtering / ratio division-by-zero / absence scope-isolation /
p99 quantile / forecast (the 6 recipe goldens under fixtures/custom_alerts_promtool/).

Skips cleanly when promtool is absent (e.g. a CI job without it); runs fully in
the dev container and any promtool-equipped environment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)
import compile_custom_alerts as cc  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "rule-packs" / "recipes" / "examples" / "conf.d"
_GOLDENS = sorted((_REPO / "tests" / "dx" / "fixtures" / "custom_alerts_promtool").glob("*.yaml"))

_PROMTOOL = shutil.which("promtool")
pytestmark = pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")


@pytest.fixture(scope="module")
def compiled_pack(tmp_path_factory):
    """Compile the example fixture into a temp pack the goldens point at."""
    workdir = tmp_path_factory.mktemp("custom_alerts_promtool")
    pack = cc.build_pack(_EXAMPLES)
    (workdir / "rule-pack-custom-alerts.yaml").write_text(
        cc._render(pack["groups"]), encoding="utf-8"
    )
    return workdir


@pytest.mark.parametrize("golden", _GOLDENS, ids=lambda p: p.stem)
def test_promtool_golden(golden, compiled_pack):
    # co-locate the golden next to the compiled pack (its rule_files is same-dir)
    dest = compiled_pack / golden.name
    dest.write_text(golden.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        [_PROMTOOL, "test", "rules", golden.name],
        cwd=compiled_pack, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"promtool failed for {golden.name}:\n{result.stdout}\n{result.stderr}"
    )


def test_goldens_present():
    """Guard against an empty glob silently passing (echo-chamber)."""
    assert len(_GOLDENS) == 7, f"expected 7 goldens (6 recipes + mode_routing), found {[p.name for p in _GOLDENS]}"
