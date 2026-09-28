"""Exit code tests for da-tools CLI tools: ``--help`` and an unknown flag.

Verifies that every registered tool:
  - exits 0 on --help
  - exits 2 (EXIT_CALLER_ERROR) on one unrecognised flag,
    ``--this-flag-does-not-exist-xyz`` (non-zero for the argparse-exempt
    tools in INVALID_ARG_EXIT2_EXEMPT)

⚠️ Scope, stated so nobody reads more into a green run than it earns:

  COVERED    ``--help`` → rc 0, and an unrecognised flag → rc 2 (non-zero for
             the exempt tools). That second path is the one argparse hands
             every tool for free — ``_lib_exitcodes.py`` says so itself — so a
             green run says little about the tools' own exit-code handling.
  NOT COVERED  a recognised flag with an unusable value: a path that does not
             exist, an invalid value, an unreachable environment (Prometheus /
             API), an IO failure. ``_lib_exitcodes.py`` files all of these
             under EXIT_CALLER_ERROR (2), and none of them is fed here. #1642
             found five such defects live (rc 0 or 1) while this file was green.
  WEAK       even the unknown-flag case, for two kinds of tool, because rc 2
             can come from a reason unrelated to the flag:
             - tools that declare a required argument: argparse's
               missing-required error exits 2 whether or not the unknown flag
               is rejected;
             - tools whose clean run already fails to reach their environment
               (e.g. backtest_threshold without Prometheus exits 2): a tool
               that swallowed unknown flags would still pass.
             The assertion only discriminates for tools whose bare run would
             exit 0. See #1642 for the measurement.
"""
import subprocess
import sys
import pytest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "scripts" / "tools"
sys.path.insert(0, str(TOOLS_DIR))
from _lib_toolcount import tool_map_scope  # noqa: E402


def collect_tools():
    """Every tool `docs/internal/tool-map.md` inventories.

    ⛔ The population is `_lib_toolcount.tool_map_scope` — the repo root plus
    ops/ dx/ lint/, skipping `_`-prefixed helpers. It used to be a second
    hand-written copy of that predicate (#1541).

    ⛔ The top level is included (#1642): ``scripts/tools/validate_all.py``
    runs the required check ``Drift Detection (validate_all.py)``, and a tool
    the gate does not enumerate can drift to any exit code unnoticed.
    """
    return [path for _subdir, path in tool_map_scope(TOOLS_DIR)]


def test_population_includes_the_top_level_runner():
    """Anti-vacuity for the #1642 fix above.

    ``TOOLS_DIR`` is part of the walk; if it were dropped again the
    parametrized sweeps below would just run fewer cases and stay green,
    which is exactly how validate_all.py stayed out of the contract for the
    life of this file. A named member of the top level is asserted to be
    enumerated, so the population cannot shrink back silently.
    """
    names = {t.name for t in ALL_TOOLS}
    assert "validate_all.py" in names, (
        "scripts/tools/validate_all.py is not in the exit-code population; "
        "the top-level walk has been lost (#1642)")
    assert not any(n.startswith("_") for n in names), (
        "a _-prefixed helper was enumerated; those are libraries, not CLIs")


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

# The argparse-exempt tools, resolved back to their Paths in ALL_TOOLS
# (sorted by name so parametrize ids are deterministic).
_ARGPARSE_EXEMPT_TOOLS = sorted(
    (t for t in ALL_TOOLS if t.name in INVALID_ARG_EXIT2_EXEMPT),
    key=lambda t: t.name,
)


def test_exempt_set_resolves_to_registered_tools():
    """Every INVALID_ARG_EXIT2_EXEMPT name must exist in ALL_TOOLS.

    Guards against a rename/move silently emptying the shrunk non-zero
    sweep below (a stale exempt name would otherwise just vanish from
    the parametrize list)."""
    assert {t.name for t in _ARGPARSE_EXEMPT_TOOLS} == INVALID_ARG_EXIT2_EXEMPT


@pytest.mark.parametrize(
    "tool_path", _ARGPARSE_EXEMPT_TOOLS,
    ids=[t.name for t in _ARGPARSE_EXEMPT_TOOLS],
)
def test_invalid_args_exits_nonzero(tool_path):
    """Argparse-exempt tools should still exit non-zero on invalid arguments.

    Deliberately runs ONLY on INVALID_ARG_EXIT2_EXEMPT: every other tool is
    already covered by test_invalid_args_exits_caller_error below, which
    asserts exit == 2 — and exit 2 implies non-zero, so a full-ALL_TOOLS
    sweep here added ~186 redundant subprocess spawns with zero extra
    coverage. This test only guards the two argparse-exempt tools that the
    exit-2 sweep skips (they exit 1 via uncaught FileNotFoundError)."""
    result = subprocess.run(
        [sys.executable, str(tool_path), "--this-flag-does-not-exist-xyz"],
        capture_output=True, timeout=10
    )
    assert result.returncode != 0, (
        f"{tool_path.name} accepted invalid args (exit code {result.returncode})"
    )


# ── #452 Track A: 0/1/2 exit-code contract ────────────────────────────
# The shared contract lives in scripts/tools/_lib_exitcodes.py:
#   0 = EXIT_OK, 1 = EXIT_VIOLATION (finding), 2 = EXIT_CALLER_ERROR.
# Below tightens the unknown-flag check from "non-zero" to "exactly 2"
# (caller error). That is ONE of the EXIT_CALLER_ERROR triggers the contract
# lists — the one argparse owns — not the contract's boundary: bad values for
# recognised flags, missing paths, unreachable environments and IO failures
# are not exercised (see the Scope block in the module docstring, #1642).
# Tools with a documented richer scheme (diag_pr_ci's 0/1/2/3,
# tenant_verify's inverted contract) still exit 2 on bad *flags* because
# argparse owns that path — so this holds for all of them.


def test_lib_exitcodes_constants_are_canonical():
    """SSOT constants must be 0/1/2 (Go da-guard/da-parser mirror)."""
    sys.path.insert(0, str(TOOLS_DIR))
    import _lib_exitcodes as ec  # noqa: E402
    assert (ec.EXIT_OK, ec.EXIT_VIOLATION, ec.EXIT_CALLER_ERROR) == (0, 1, 2)


@pytest.mark.parametrize("tool_path", ALL_TOOLS, ids=[t.name for t in ALL_TOOLS])
def test_invalid_args_exits_caller_error(tool_path):
    """Unrecognised flags must exit exactly 2 (EXIT_CALLER_ERROR), not just
    any non-zero (#452 Track A). Only the unknown-flag path; for tools with
    a required argument or an unreachable environment, rc 2 may come from
    that instead (see the module docstring's Scope block, #1642)."""
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
