"""Shell scripts that run the route generator provide every Go reader it needs.

`generate_alertmanager_routes.py` stops with rc 2 when da-guard, da-read or
da-crdecode is missing. `scripts/benchmark.sh` used to build da-guard only, so
its routing bench died on a machine without the other two. The set of readers
is measured here from the generator itself (each one blanked in turn), and
every `scripts/*.sh` that runs the generator must ensure each of them through
`scripts/ops/_da_binaries.sh`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
HELPER = REPO / "scripts" / "ops" / "_da_binaries.sh"
CONF_D = REPO / "components" / "threshold-exporter" / "config" / "conf.d"
_BASH = shutil.which("bash") or "bash"
READERS = {"da-guard": "DA_GUARD_BINARY", "da-read": "DA_READ_BINARY",
           "da-crdecode": "DA_CRDECODE_BINARY"}


def _have_all_readers() -> dict[str, str]:
    found = {}
    for name, var in READERS.items():
        path = os.environ.get(var) or shutil.which(name) or str(REPO / ".build" / name)
        if not Path(path).is_file():
            pytest.skip(f"{name} not available ({var} unset, not on PATH, no .build/{name})")
        found[var] = path
    return found


def _needed_readers() -> set[str]:
    """The readers the generator cannot run without: blank one at a time
    (PATH without any of them) and see whether it stops naming that one."""
    base = _have_all_readers()
    needed = set()
    for name, var in READERS.items():
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"),
               **base, var: ""}
        proc = subprocess.run(
            [sys.executable, "-s", str(GAR), "--config-dir", str(CONF_D),
             "--output-alertmanager", "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
        if proc.returncode == 2 and name in proc.stderr:
            needed.add(name)
    return needed


def _scripts_running_the_generator() -> list[Path]:
    return sorted(p for p in (REPO / "scripts").glob("*.sh")
                  if "generate_alertmanager_routes.py" in p.read_text(encoding="utf-8"))


def test_the_generator_needs_all_three_readers():
    """Control for the test below: if the generator stops needing one, the
    set shrinks and so does what the scripts must provide."""
    assert _needed_readers() == set(READERS)


@pytest.mark.parametrize("script", _scripts_running_the_generator(),
                         ids=lambda p: p.name)
def test_a_script_running_the_generator_ensures_each_reader(script):
    text = script.read_text(encoding="utf-8")
    assert "_da_binaries.sh" in text, f"{script.name} does not source scripts/ops/_da_binaries.sh"
    ensured = set(re.findall(r"^\s*ensure_da_binary\s+(da-[a-z]+)\b", text, re.M))
    missing = _needed_readers() - ensured
    assert not missing, f"{script.name} runs the route generator without ensuring {sorted(missing)}"


def test_control_the_scripts_are_found():
    names = {p.name for p in _scripts_running_the_generator()}
    assert {"benchmark.sh", "demo-showcase.sh"} <= names


# ── the helper ────────────────────────────────────────────────────────

def _run_helper(tmp_path: Path, name: str, env_value: str | None, make_rc: int = 0):
    """Source the helper with a fake `make` that records its argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "make.log"
    (bindir / "make").write_text(
        f"#!/bin/sh\necho \"$@\" >> {log}\nexit {make_rc}\n", encoding="utf-8")
    (bindir / "make").chmod(0o755)
    var = READERS[name]
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    if env_value is not None:
        env[var] = env_value
    script = f'. "{HELPER}"; ensure_da_binary {name} && printf "%s" "${{{var}}}"'
    proc = subprocess.run([_BASH, "-c", script], capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60)
    made = log.read_text(encoding="utf-8") if log.exists() else ""
    return proc, made


@pytest.mark.parametrize("name", sorted(READERS))
def test_a_set_variable_is_used_and_nothing_is_built(tmp_path, name):
    proc, made = _run_helper(tmp_path, name, "/opt/given/" + name)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "/opt/given/" + name
    assert made == ""


@pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "empty", "blank"])
@pytest.mark.parametrize("name", sorted(READERS))
def test_an_unset_or_blank_variable_builds_it_and_exports_the_path(tmp_path, name, value):
    proc, made = _run_helper(tmp_path, name, value)
    assert proc.returncode == 0, proc.stderr
    assert f"{name}-build" in made
    assert proc.stdout == str(REPO / ".build" / name)


@pytest.mark.parametrize("name", sorted(READERS))
def test_a_failed_build_stops_naming_the_reader(tmp_path, name):
    proc, _ = _run_helper(tmp_path, name, None, make_rc=2)
    assert proc.returncode != 0
    assert name in proc.stderr and READERS[name] in proc.stderr
