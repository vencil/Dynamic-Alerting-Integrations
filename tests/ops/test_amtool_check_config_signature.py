"""amtool check-config's text signature, pinned to recorded outputs (#3611 Q2).

``_grar_render.amtool_verdict`` is the one place the repo reads amtool's
output text: amtool exits 1 for every failure, so ``FAILED:`` is the only
thing that tells "the config is wrong" from "amtool could not run". The owner
allowed that one text exception on condition it is pinned to the version that
ships, by fixture (ruling 2026-11-06). This module is that pin:

* ``recorded.json`` holds what the pinned amtool printed for each input in
  this directory (rc + combined stdout/stderr), and the verdict each must get;
* the recorded version must equal the Alertmanager image the manifests pin
  (``k8s/03-monitoring/deployment-alertmanager.yaml``, the version SSOT the
  da-tools image and CI's ``Install amtool`` step both read), so bumping it
  without re-recording turns this red;
* where amtool is installed (CI's Python Tests jobs, ``VIBE_REQUIRE_AMTOOL=1``)
  the recordings are replayed and must match byte for byte.

To re-record after a bump: run the pinned ``amtool check-config <input>`` from
this directory for each case and replace ``rc`` / ``output`` (the replay test's
failure message prints the new entry).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _tree import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
import _grar_render as R  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "amtool_check_config"
RECORDED = json.loads((FIXTURES / "recorded.json").read_text(encoding="utf-8"))
MANIFEST = REPO_ROOT / "k8s" / "03-monitoring" / "deployment-alertmanager.yaml"


def _pinned_version() -> str:
    tags = re.findall(r"prom/alertmanager:v(\d+\.\d+\.\d+)",
                      MANIFEST.read_text(encoding="utf-8"))
    assert len(set(tags)) == 1, f"expected one pinned prom/alertmanager tag in {MANIFEST}: {tags}"
    return tags[0]


def test_the_recordings_are_of_the_pinned_amtool():
    """A bump of the Alertmanager pin without re-recording turns this red."""
    assert RECORDED["amtool_version"] == _pinned_version(), (
        "re-record tests/ops/fixtures/amtool_check_config/recorded.json with the "
        "newly pinned amtool (see this module's docstring)")


def test_every_verdict_is_recorded():
    assert {c["verdict"] for c in RECORDED["cases"]} == {
        R.AMTOOL_ACCEPTED, R.AMTOOL_REJECTED, R.AMTOOL_UNUSABLE}


@pytest.mark.parametrize("case", RECORDED["cases"], ids=lambda c: c["input"])
def test_each_recorded_output_gets_its_verdict(case):
    assert R.amtool_verdict(case["rc"], case["output"], case["input"]) == case["verdict"], case


def test_a_file_amtool_could_not_open_is_not_a_rejection():
    """`FAILED: open <path>: …` is amtool failing to READ, rc 1 like a real
    rejection (measured, 0.34.2). Reading it as `rejected` would tell the user
    to fix a config nobody looked at."""
    [case] = [c for c in RECORDED["cases"] if c["input"] == "noperm.yml"]
    assert "FAILED: open noperm.yml:" in case["output"] and case["rc"] == 1
    assert R.amtool_verdict(case["rc"], case["output"], "noperm.yml") == R.AMTOOL_UNUSABLE
    # the exception is anchored on the path amtool was given, not on any "open"
    other = case["output"].replace("open noperm.yml:", "open elsewhere.yml:")
    assert R.amtool_verdict(1, other, "noperm.yml") == R.AMTOOL_REJECTED


_LIVE = [c for c in RECORDED["cases"] if c["input"] != "noperm.yml"]


@pytest.mark.usefixtures("amtool_required")
@pytest.mark.parametrize("case", _LIVE, ids=lambda c: c["input"])
def test_the_installed_amtool_still_prints_the_recording(case, tmp_path):
    """Replays each recorded input (from a copy of this directory, so the
    names amtool echoes are the recorded ones)."""
    version = subprocess.run([shutil.which("amtool"), "--version"], capture_output=True,
                             text=True, encoding="utf-8", timeout=30).stdout.split()[2]
    assert version == RECORDED["amtool_version"], (version, RECORDED["amtool_version"])
    for f in FIXTURES.glob("*.yml"):
        shutil.copy(f, tmp_path / f.name)
    proc = subprocess.run([shutil.which("amtool"), "check-config", case["input"]], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    got = {"input": case["input"], "rc": proc.returncode, "output": proc.stdout + proc.stderr}
    assert (got["rc"], got["output"]) == (case["rc"], case["output"]), (
        "the installed amtool no longer prints what was recorded; new entry:\n"
        + json.dumps(got, ensure_ascii=False))


def test_a_failed_line_from_an_exit_other_than_1_is_not_a_rejection():
    """A Go panic exits 2; whatever it printed, amtool gave no verdict."""
    assert R.amtool_verdict(2, "FAILED: something\npanic: boom", "x.yml") == R.AMTOOL_UNUSABLE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for amtool")
def test_amtool_check_reads_its_run_through_amtool_verdict(tmp_path, monkeypatch):
    """End to end through `amtool_check`: a stand-in amtool that cannot open
    the file it is handed prints what the pinned one prints (recorded above),
    for the exact path `amtool_check` passed — no verdict, exit 2."""
    stub = tmp_path / "amtool"
    stub.write_text("#!/bin/sh\n"
                    "printf \"Checking '%s'  FAILED: open %s: permission denied\\n\\n"
                    "amtool: error: failed to validate 1 file(s)\\n\\n\" \"$2\" \"$2\"\n"
                    "exit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    check = R.amtool_check("route:\n  receiver: r\nreceivers:\n- name: r\n",
                           what="the config", refusing="write it")
    assert (check.status, check.exit_code) == (R.AMTOOL_UNUSABLE, 2), check
