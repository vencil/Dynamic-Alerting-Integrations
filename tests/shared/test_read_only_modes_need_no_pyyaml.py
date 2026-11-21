"""The read-only modes of three tools run without PyYAML (#3981).

`check_repo_name --ci` and `check_doc_links --ci` are `language: python`
pre-commit hooks with no `additional_dependencies`: their environment has no
PyYAML. `add_frontmatter --check` runs in docs-ci. Their WRITING modes go
through `_lib_io.write_repo_artifact` since #3981, and `_lib_io` imports
PyYAML at the top — so each tool imports it only on the path that writes.
The first cut imported it at the top of the tool; the `repo-name-check` hook
died on `import yaml`, and `doc-links-check` would have on the next docs
commit.

The control is of the HARNESS, not of the tools: a script that imports PyYAML
must fail under it, or "rc 0 without PyYAML" would be measuring nothing.
(Review: the first control asserted that each tool's WRITING mode fails
without PyYAML — which pinned a limitation as the expected behaviour.)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"

# Runs the script named by argv[1] as `__main__`, with `import yaml` refused.
_NO_YAML = """
import builtins, runpy, sys
real = builtins.__import__
def refuse(name, *a, **k):
    if name == "yaml" or name.startswith("yaml."):
        raise ModuleNotFoundError("No module named 'yaml' (refused by the test)")
    return real(name, *a, **k)
builtins.__import__ = refuse
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _tree(tmp_path: Path) -> Path:
    """A directory both doc tools accept: `docs/` with one Markdown file that
    has front matter and no link."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "zdoc.md").write_text("---\ntitle: z\n---\n\n# z\n", encoding="utf-8")
    return tmp_path


def _run(script: str | Path, argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _NO_YAML, str(TOOLS / script), *argv], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=300)


ROWS = [
    # script, read-only argv ("{tree}" is the temp tree)
    ("lint/check_repo_name.py", ["--ci", "--full-scan"]),
    ("lint/check_repo_name.py", ["--ci"]),
    # A caller error is named as one here too, not buried under an import.
    ("lint/check_repo_name.py", ["--fix"]),
    ("lint/check_doc_links.py", ["--ci", "--repo-root", "{tree}"]),
    ("dx/add_frontmatter.py", ["--check", "--base-dir", "{tree}/docs"]),
    ("dx/add_frontmatter.py", ["--dry-run", "--base-dir", "{tree}/docs"]),
]


@pytest.mark.parametrize("script, argv", ROWS, ids=[f"{r[0]} {' '.join(r[1])}" for r in ROWS])
def test_the_read_only_mode_runs_without_pyyaml(tmp_path, script, argv):
    tree = _tree(tmp_path)
    cwd = REPO_ROOT if script.endswith("check_repo_name.py") else tree
    done = _run(script, [a.replace("{tree}", str(tree)) for a in argv], cwd)
    assert "No module named" not in done.stderr, done.stderr
    if argv == ["--fix"]:
        assert done.returncode == 2 and "--fix requires --full-scan" in done.stderr, done.stderr
    else:
        assert done.returncode == 0, done.stderr


def test_the_harness_does_refuse_pyyaml(tmp_path):
    """The control. Without it every row above could pass because the
    refusal did nothing."""
    probe = tmp_path / "zprobe.py"
    probe.write_text("import yaml\nprint('imported')\n", encoding="utf-8")
    done = _run(probe, [], tmp_path)
    assert done.returncode != 0 and "refused by the test" in done.stderr, (
        done.returncode, done.stdout, done.stderr)
    assert "imported" not in done.stdout
