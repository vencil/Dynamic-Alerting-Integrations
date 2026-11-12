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
import shutil
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


def _phases() -> list[tuple[str, str, str]]:
    """(phase id, command, first terminal line) for every phase entry."""
    src = TRANSCRIPTS.read_text(encoding="utf-8")
    return re.findall(
        r"^  (\w+): \{\n    command: '([^']+)',\n    terminal: \[\n      '([^']*)',$",
        src, flags=re.MULTILINE)


def test_every_command_belongs_to_a_parsed_phase():
    # Keeps the per-phase check below from silently covering fewer phases
    # than there are displayed commands (e.g. after a layout change).
    assert [c for _, c, _ in _phases()] == _displayed_commands()


@pytest.mark.parametrize("phase,command,first_line", _phases())
def test_terminal_echoes_its_own_command(phase, command, first_line):
    # Paired per phase: a whole-file substring search would pass if one
    # phase lost its echo while another phase carried the same one.
    assert first_line == f"$ {command}", (
        f"{phase}: terminal playback starts with {first_line!r}, "
        f"not `$ {command}`")


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


# ── migrate: the displayed output is a fresh run's output ───────────────
#
# The argv check above cannot see the output: migrate's playback kept
# showing the old flat `tenant-config.yaml` ("copy into conf.d/<tenant>.yaml")
# long after the tool started writing `.conf.d/<tenant>.yaml` (thresholds
# under `tenants.<tenant>`) + a `defaults:` fragment for the root
# `_defaults.yaml` (#3698, #3759). This runs the displayed command on the
# fixture the transcript names and compares terminal + sample verbatim.

_LEGACY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legacy-dummy.yml"
# Order the sample shows them in; each under a `# ── <path> ──` line.
_MIGRATE_SAMPLE_FILES = ("demo-tenant.yaml", "_defaults.yaml")


def _js_template_value(raw: str) -> str:
    """Value of a JS template literal body that has no `${…}` interpolation."""
    return re.sub(r"\\(.)", r"\1", raw, flags=re.DOTALL)


def _js_template_body(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _migrate_block() -> tuple[str, list[str], str]:
    src = TRANSCRIPTS.read_text(encoding="utf-8")
    m = re.search(
        r"^  migrate: \{\n    command: '([^']+)',\n    terminal: \[\n(.*?)\n    \],\n"
        r"    sample: `((?:[^`\\]|\\.)*)`,\n  \},$",
        src, flags=re.MULTILINE | re.DOTALL)
    assert m, "migrate block not found in the shape this test reads"
    lines = re.findall(r"^      '((?:[^'\\]|\\.)*)',$", m.group(2), flags=re.MULTILINE)
    assert len(lines) == m.group(2).count("\n") + 1, "unparsed terminal line"
    assert "${" not in m.group(3).replace("\\${", ""), "sample interpolates"
    return (m.group(1), [re.sub(r"\\(.)", r"\1", ln) for ln in lines],
            _js_template_value(m.group(3)))


def test_migrate_playback_matches_a_fresh_run(tmp_path):
    command, terminal, sample = _migrate_block()
    argv = shlex.split(command)
    assert argv[:3] == ["da-tools", "migrate", "legacy_rules.yaml"], command
    out_dir = argv[argv.index("-o") + 1]
    shutil.copyfile(_LEGACY_FIXTURE, tmp_path / "legacy_rules.yaml")

    # Through the da-tools dispatcher, as the page shows it being typed.
    proc = subprocess.run(
        [sys.executable, entrypoint.__file__, *argv[1:]],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", timeout=120,
        cwd=tmp_path, env={**os.environ, "DA_LANG": "en"},
    )
    assert proc.returncode == 0, f"`{command}` rc={proc.returncode}\n{proc.stdout[-1500:]}"

    conf_d = tmp_path / out_dir / ".conf.d"
    written = sorted(p.name for p in conf_d.iterdir())
    assert written == sorted(_MIGRATE_SAMPLE_FILES), written
    fresh_terminal = [f"$ {command}", "", *proc.stdout.splitlines()]
    fresh_sample = "\n".join(
        f"# ── {out_dir}/.conf.d/{name} ──\n"
        + (conf_d / name).read_text(encoding="utf-8").rstrip("\n")
        for name in _MIGRATE_SAMPLE_FILES)

    regenerated = (
        "    terminal: [\n"
        + "".join("      '%s',\n" % ln.replace("\\", "\\\\").replace("'", "\\'")
                  for ln in fresh_terminal)
        + "    ],\n    sample: `" + _js_template_body(fresh_sample) + "`,")
    hint = f"re-capture {TRANSCRIPTS.name} migrate block from a fresh run:\n{regenerated}"
    assert terminal == fresh_terminal, hint
    assert sample == fresh_sample, hint
