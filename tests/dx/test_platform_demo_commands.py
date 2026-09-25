"""Guard: the portal Platform Demo only displays da-tools commands the real CLI accepts.

The portal's Platform Demo plays back canned terminal output (no fetch).
Its commands used to drift from the CLI — flags that never existed
(``migrate --from/--to``), a subcommand mapped to a different tool
(``validate`` is validate_migration.py, not config validation), and an
argparse-ambiguous prefix (``generate-routes --config``). All three exit 2
on a real run, while the page showed "Status: SUCCESS".

For each displayed ``command`` this resolves the script the da-tools
dispatcher would run, then feeds the argv through that script's own
argparse parser — and stops right after parsing, so nothing is executed
(``baseline`` would otherwise block on Prometheus for minutes). An unknown
flag, an ambiguous prefix or a missing required argument exits 2 → red.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import entrypoint  # noqa: E402  (path set by conftest.py)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTS = (
    REPO_ROOT / "tools" / "portal" / "src" / "interactive" / "tools"
    / "platform-demo" / "fixtures" / "transcripts.js"
)

# Runs the target script as __main__ but exits the moment argparse has
# accepted argv: 0 = parsed, 2 = argparse rejected it, 3 = never parsed.
_PARSE_ONLY_DRIVER = """
import argparse, os, runpy, sys
_orig = argparse.ArgumentParser.parse_args
def _parse_then_exit(self, *a, **k):
    _orig(self, *a, **k)
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)
argparse.ArgumentParser.parse_args = _parse_then_exit
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(script))
runpy.run_path(script, run_name="__main__")
os._exit(3)
"""


def _displayed_commands() -> list[str]:
    src = TRANSCRIPTS.read_text(encoding="utf-8")
    return re.findall(r"^\s*command: '([^']+)',$", src, flags=re.MULTILINE)


def test_demo_has_commands():
    # A regex that silently matches nothing would make every check below vacuous.
    assert len(_displayed_commands()) >= 5


@pytest.mark.parametrize("command", _displayed_commands())
def test_terminal_echoes_the_displayed_command(command):
    src = TRANSCRIPTS.read_text(encoding="utf-8")
    assert f"'$ {command}'," in src, (
        f"terminal playback does not start with `$ {command}`")


@pytest.mark.parametrize("command", _displayed_commands())
def test_displayed_command_parses_with_real_cli(command):
    argv = shlex.split(command)
    assert argv[0] == "da-tools", command
    sub, args = argv[1], argv[2:]
    assert sub in entrypoint.COMMAND_MAP, f"unknown da-tools subcommand: {sub}"
    script, searched = entrypoint._resolve_script_path(entrypoint.COMMAND_MAP[sub])
    assert script, f"{sub}: script not found in {searched}"

    proc = subprocess.run(
        [sys.executable, "-c", _PARSE_ONLY_DRIVER, script, *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        cwd=REPO_ROOT, env={**os.environ, "DA_LANG": "en"},
    )
    assert proc.returncode == 0, (
        f"`{command}` → {os.path.basename(script)} rc={proc.returncode}\n"
        f"{proc.stderr[-800:]}")
