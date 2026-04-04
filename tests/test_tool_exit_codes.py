"""Exit code contract tests for da-tools CLI tools.

Verifies that every registered tool:
  - exits 0 on --help
  - exits non-zero on invalid arguments
"""
import subprocess
import sys
import pytest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent / "scripts" / "tools"
OPS_DIR = TOOLS_DIR / "ops"
DX_DIR = TOOLS_DIR / "dx"
LINT_DIR = TOOLS_DIR / "lint"


def collect_tools():
    """Collect all .py tool files from ops, dx, lint subdirectories.

    Excludes files starting with underscore and __init__.py.
    """
    tools = []
    for d in [OPS_DIR, DX_DIR, LINT_DIR]:
        if d.is_dir():
            for f in sorted(d.glob("*.py")):
                if f.name.startswith("_") or f.name == "__init__.py":
                    continue
                tools.append(f)
    return tools


ALL_TOOLS = collect_tools()


@pytest.mark.parametrize("tool_path", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_help_exits_zero(tool_path):
    """Every tool should exit 0 on --help."""
    result = subprocess.run(
        [sys.executable, str(tool_path), "--help"],
        capture_output=True, timeout=10
    )
    assert result.returncode == 0, (
        f"{tool_path.name} --help failed with exit code {result.returncode}\n"
        f"stderr: {result.stderr.decode()[:300]}"
    )


@pytest.mark.parametrize("tool_path", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_invalid_args_exits_nonzero(tool_path):
    """Tools should exit non-zero on invalid arguments."""
    result = subprocess.run(
        [sys.executable, str(tool_path), "--this-flag-does-not-exist-xyz"],
        capture_output=True, timeout=10
    )
    # argparse exits 2 on unrecognized args
    assert result.returncode != 0, (
        f"{tool_path.name} accepted invalid args (exit code {result.returncode})"
    )
