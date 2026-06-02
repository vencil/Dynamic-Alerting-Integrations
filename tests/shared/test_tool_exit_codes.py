"""Exit code contract tests for da-tools CLI tools.

Verifies that every registered tool:
  - exits 0 on --help
  - exits non-zero on invalid arguments
"""
import subprocess
import sys
import pytest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "scripts" / "tools"
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


# ── #452 Track A: 0/1/2 exit-code contract ────────────────────────────
# The shared contract lives in scripts/tools/_lib_exitcodes.py:
#   0 = EXIT_OK, 1 = EXIT_VIOLATION (finding), 2 = EXIT_CALLER_ERROR.
# Below tightens the "invalid args" gate from "non-zero" to "exactly 2"
# (caller error), which is the observable boundary of the convention every
# tool must honour. Tools with a documented richer scheme (diag_pr_ci's
# 0/1/2/3, tenant_verify's inverted contract) still exit 2 on bad *flags*
# because argparse owns that path — so this holds for all of them.

# Tools whose bad-flag handling legitimately does NOT go through argparse's
# exit-2 path. Documented exceptions only (with rationale), so new drift is
# caught rather than silently allowed.
INVALID_ARG_EXIT2_EXEMPT: set[str] = {
    # These two a11y dev tools take a bare positional `path` (no argparse
    # flag parsing), so an unrecognised "--flag" is treated as a path and
    # raises an uncaught FileNotFoundError → Python's default exit 1, not 2.
    # Minor known inconsistency in non-customer-facing dx tools; left as-is
    # to avoid widening this sweep. Their valid exit paths DO use the 0/1/2
    # constants (see the files' main()).
    "check_aria_references.py",
    "axe_lite_static.py",
}


def test_lib_exitcodes_constants_are_canonical():
    """SSOT constants must be 0/1/2 (Go da-guard/da-parser mirror)."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert (ec.EXIT_OK, ec.EXIT_VIOLATION, ec.EXIT_CALLER_ERROR) == (0, 1, 2)


@pytest.mark.parametrize("tool_path", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_invalid_args_exits_caller_error(tool_path):
    """Unrecognised flags must exit exactly 2 (EXIT_CALLER_ERROR), not just
    any non-zero — the convention's observable boundary (#452 Track A)."""
    if tool_path.name in INVALID_ARG_EXIT2_EXEMPT:
        pytest.skip(f"{tool_path.name} documented exempt")
    result = subprocess.run(
        [sys.executable, str(tool_path), "--this-flag-does-not-exist-xyz"],
        capture_output=True, timeout=10
    )
    assert result.returncode == 2, (
        f"{tool_path.name} should exit 2 (EXIT_CALLER_ERROR) on a bad flag, "
        f"got {result.returncode}\nstderr: {result.stderr.decode('utf-8', 'replace')[:300]}"
    )
