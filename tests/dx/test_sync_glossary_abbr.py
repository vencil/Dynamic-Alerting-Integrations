"""sync_glossary_abbr: the snippet it regenerates is a tracked file (#3107).

`docs/includes/abbreviations.md` is committed 0644. The tool used to write it
with a name that ends every file 0600 — a change `git status` does not show,
since git records the execute bit and nothing else of a mode.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "tools" / "dx" / "sync_glossary_abbr.py"
GLOSSARY = ("# Glossary\n\n**SLO (Service Level Objective)** — a target.\n\n"
            "**SLA (Service Level Agreement)** — a promise.\n")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _tree(tmp_path: Path, snippet: "str | None", mode: int = 0o644) -> Path:
    """A docs tree laid out as the tool's defaults expect it, in *tmp_path*."""
    docs = tmp_path / "docs"
    (docs / "includes").mkdir(parents=True)
    (docs / "glossary.md").write_text(GLOSSARY, encoding="utf-8")
    out = docs / "includes" / "abbreviations.md"
    if snippet is not None:
        out.write_text(snippet, encoding="utf-8")
        out.chmod(mode)
    return out


def _run(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *argv], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


@pytest.mark.parametrize("mode", [0o644, 0o755], ids=oct)
def test_a_sync_leaves_the_snippet_with_the_mode_it_was_checked_out_with(tmp_path, mode):
    out = _tree(tmp_path, "stale\n", mode)
    r = _run(tmp_path)  # the defaults: docs/glossary.md → docs/includes/abbreviations.md
    assert r.returncode == 0, r.stderr
    assert "*[SLO]: Service Level Objective" in out.read_text(encoding="utf-8")
    assert _mode(out) == mode
    assert _run(tmp_path, "--check").returncode == 0


def test_a_first_sync_makes_the_snippet_0644(tmp_path):
    out = _tree(tmp_path, None)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert _mode(out) == 0o644
