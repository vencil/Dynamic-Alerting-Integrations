"""amtool check-config's text signature, pinned to recorded outputs (#3611 Q2).

``_grar_render.amtool_verdict`` is the one place the repo reads amtool's
output text: amtool exits 1 for every failure, so ``FAILED:`` is the only
thing that tells "the config is wrong" from "amtool could not run". The owner
allowed that one text exception on condition it is pinned to the version that
ships, by fixture (ruling 2026-11-06). This module is that pin:

* ``recorded.json`` holds what the pinned amtool printed for each case (rc +
  combined stdout/stderr, run from a copy of this directory) and the verdict
  each must get. A case's ``setup`` is what the directory needs besides its
  files: ``directory:<name>`` (a directory where a template is expected) or
  ``unreadable`` (the input written with mode 000);
* the recorded version must equal the Alertmanager image the manifests pin
  (``k8s/03-monitoring/deployment-alertmanager.yaml``, the version SSOT the
  da-tools image and CI's ``Install amtool`` step both read), so bumping it
  without re-recording turns this red;
* where amtool is installed (CI's Python Tests jobs, ``VIBE_REQUIRE_AMTOOL=1``)
  every case is replayed and must match byte for byte — ``unreadable`` only
  as a non-root user (root reads a mode-000 file; CI's runner is not root).

To re-record after a bump: run the pinned ``amtool check-config <input>`` the
same way for each case and replace ``rc`` / ``output`` (the replay test's
failure message prints the new entry).
"""
from __future__ import annotations

import json
import os
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
_CASES = RECORDED["cases"]


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
    assert {c["verdict"] for c in _CASES} == {
        R.AMTOOL_ACCEPTED, R.AMTOOL_REJECTED, R.AMTOOL_UNUSABLE}


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["input"])
def test_each_recorded_output_gets_its_verdict(case):
    assert R.amtool_verdict(case["rc"], case["output"]) == case["verdict"], case


def test_a_file_amtool_could_not_read_is_never_a_rejection():
    """`FAILED: open <path>:` / `FAILED: read <path>:` — the config file or a
    template it names — is rc 1 exactly like a rejection (measured, 0.34.2).
    Reading it as `rejected` would tell the user to fix a config nobody read."""
    io = [c for c in _CASES if c["setup"] or c["input"].startswith("/proc/")]
    assert {c["input"] for c in io} == {"/proc/self/mem", "template_dir.yml", "noperm.yml"}
    for case in io:
        assert case["rc"] == 1 and "FAILED:" in case["output"]
        assert R.amtool_verdict(1, case["output"]) == R.AMTOOL_UNUSABLE, case


def test_a_failed_line_from_an_exit_other_than_1_is_not_a_rejection():
    """A Go panic exits 2; whatever it printed, amtool gave no verdict."""
    assert R.amtool_verdict(2, "FAILED: something\npanic: boom") == R.AMTOOL_UNUSABLE


def _replayable(case):
    if case["setup"] == "unreadable" and os.geteuid() == 0:
        return pytest.param(case, marks=pytest.mark.skip(
            reason="root reads a mode-000 file; CI's runner user is not root"))
    if case["input"].startswith("/proc/") and not Path(case["input"]).exists():
        return pytest.param(case, marks=pytest.mark.skip(reason="no /proc here"))
    return case


@pytest.mark.usefixtures("amtool_required")
@pytest.mark.parametrize("case", [_replayable(c) for c in _CASES], ids=lambda c: c["input"])
def test_the_installed_amtool_still_prints_the_recording(case, tmp_path):
    """Replays each case from a copy of this directory, so the names amtool
    echoes are the recorded ones."""
    amtool = shutil.which("amtool")
    version = subprocess.run([amtool, "--version"], capture_output=True,
                             text=True, encoding="utf-8", timeout=30).stdout.split()[2]
    assert version == RECORDED["amtool_version"], (version, RECORDED["amtool_version"])
    for f in [*FIXTURES.glob("*.yml"), *FIXTURES.glob("*.tmpl")]:
        shutil.copy(f, tmp_path / f.name)
    setup = case["setup"] or ""
    if setup.startswith("directory:"):
        (tmp_path / setup.split(":", 1)[1]).mkdir()
    if setup == "unreadable":
        target = tmp_path / case["input"]
        target.write_text("route:\n  receiver: r\nreceivers:\n- name: r\n", encoding="utf-8")
        target.chmod(0)
    proc = subprocess.run([amtool, "check-config", case["input"]], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    got = {"input": case["input"], "setup": case["setup"], "rc": proc.returncode,
           "output": proc.stdout + proc.stderr}
    assert (got["rc"], got["output"]) == (case["rc"], case["output"]), (
        "the installed amtool no longer prints what was recorded; new entry:\n"
        + json.dumps(got, ensure_ascii=False))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for amtool")
@pytest.mark.parametrize("failure", ["open %s: permission denied", "read %s: input/output error",
                                     "read /etc/am/tmpl/x.tmpl: is a directory"])
def test_amtool_check_reads_its_run_through_amtool_verdict(tmp_path, monkeypatch, failure):
    """End to end through `amtool_check`: a stand-in amtool printing what the
    pinned one prints when it cannot read the config (or a template it names)
    gives no verdict, exit 2 — not "rejected"."""
    line = failure % "$2" if "%s" in failure else failure
    stub = tmp_path / "amtool"
    stub.write_text("#!/bin/sh\n"
                    f"printf \"Checking '%s'  FAILED: {line}\\n\\n"
                    "amtool: error: failed to validate 1 file(s)\\n\\n\" \"$2\"\n"
                    "exit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    check = R.amtool_check("route:\n  receiver: r\nreceivers:\n- name: r\n",
                           what="the config", refusing="write it")
    assert (check.status, check.exit_code) == (R.AMTOOL_UNUSABLE, 2), check
