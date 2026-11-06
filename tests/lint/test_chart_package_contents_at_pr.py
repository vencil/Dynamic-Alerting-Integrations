"""Run the release-time chart package check on every PR (#1018).

`scripts/tools/lint/check_chart_package_contents.py` asserts what a packaged
chart .tgz contains, but it runs only in release.yaml and `make chart-package`
— after `helm package`, right before `helm push`. A PR that adds a directory
to a shipping chart (the Pod Security gate's `ci/` value sets did exactly
that) stays green until release night. This packages each shipping chart the
way release does and runs the same check, so the PR goes red instead.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools" / "lint"))
from check_chart_package_contents import DECLARED_SUBTREES, check_archive  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not on PATH")


@pytest.mark.parametrize("chart", sorted(DECLARED_SUBTREES))
def test_packaged_chart_carries_only_declared_contents(chart: str, tmp_path: Path) -> None:
    out = subprocess.run(["helm", "package", str(REPO / chart), "-d", str(tmp_path)],
                         capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert out.returncode == 0, out.stderr
    tgz = sorted(tmp_path.glob("*.tgz"))
    assert len(tgz) == 1, tgz
    problems = check_archive(chart, tgz[0])
    assert not problems, "\n".join(problems)
