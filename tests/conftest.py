"""Shared test fixtures and sys.path setup for da-tools test suite.

This conftest.py is loaded automatically by pytest before any test module.
It sets up sys.path so every test can ``import scaffold_tenant``, ``import
_lib_python``, ``import bump_docs``, etc. without per-file boilerplate.

Factory helpers are defined in ``tests/factories.py``.
"""
import os
import sys
import tempfile

import pytest

# ── sys.path: scripts/tools + all subdirs ────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_DIR = os.path.join(REPO_ROOT, "scripts", "tools")

for _path in [
    _TOOLS_DIR,
    os.path.join(_TOOLS_DIR, "ops"),
    os.path.join(_TOOLS_DIR, "dx"),
    os.path.join(_TOOLS_DIR, "lint"),
]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

# ── sys.path: da-tools entrypoint (for test_entrypoint.py) ──────────
_DA_TOOLS_DIR = os.path.join(
    REPO_ROOT, "components", "da-tools", "app",
)
if _DA_TOOLS_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_DA_TOOLS_DIR))

# ── Local imports (factories helper) ──────────────────────────────────
from factories import populate_routing_dir  # noqa: E402


# ── Hypothesis: repo-wide settings profile ───────────────────────────
#
# Repo default: NO per-example deadline. A deadline is an OPT-IN tripwire
# a test may add for itself (see the rule at the bottom of this block).
#
# Why: Hypothesis's stock 200ms per-example deadline measures the HOST'S
# I/O SCHEDULER, not the property under test, for any example that touches
# the filesystem — and the FIRST example of a run additionally pays warm-up
# cost. When that first call crosses the budget but the replay does not,
# Hypothesis raises `FlakyFailure: ... Falsified on the first call but did
# not on a subsequent one`. That is a timing artefact, not a property
# violation.
#
# The giveaway: tests/ops/test_property_based.py::TestFileSha256::
# test_same_content_same_hash asserts "identical content produces an
# identical hash" — a property that CANNOT be falsified — yet it failed at
# 290.92ms against the 200ms deadline (replay: 2.11ms).
#
# Measured scope, stated honestly so nobody over-reads it:
#   - Reproduces on a Windows dev host, but only UNDER LOAD. It is not
#     reliably reproducible: a later idle sweep on the same host ran 14
#     consecutive green runs at the stock deadline. Treat it as a
#     load-dependent flake, NOT a fixed failure rate of the test.
#   - A Linux container stayed green over 5 quiet runs and 3 full `-n 16`
#     contention runs (7235 tests each) at the stock deadline, and recent
#     CI "Python Tests" failures were traced to unrelated causes.
# So this is a DX fix that also removes a LATENT CI risk, not a fix for an
# active CI fire. The latent risk is real: the module is NOT in the
# --ignore list of the job that actually runs pytest — `Python Tests —
# run (3.13)` (ci.yml; "Python Tests (3.13)" is the aggregation gate that
# runs no tests) — and that job uses `-n auto` with `-x`, so if the timing
# margin ever closes there, one trip reddens a required check.
#
# What this does NOT replace: CI's `pytest-timeout --timeout=300` is a
# per-test HANG backstop only. It is 1500x coarser than a per-example
# deadline and cannot catch the algorithmic regression a deadline is meant
# to catch — it is a different guarantee, not an equivalent substitute.
# `HealthCheck.too_slow` does still apply to tests that don't suppress it.
#
# THE RULE (apply per test, never per file):
#   - touches I/O            -> no deadline (this profile's default)
#   - pure function, and you
#     want a perf tripwire   -> opt in with an explicit
#                               `@settings(deadline=...)`, which OVERRIDES
#                               this profile.
# tests/shared/test_property_tools.py does exactly that: 79 pure-function
# properties opt into deadline=500, while its 3 filesystem-touching ones
# opt back out via PILOT_SETTINGS_IO — one of those was observed failing
# at 916.75ms against that same 500ms budget.
#
# Escape hatch: `HYPOTHESIS_PROFILE=strict` restores the stock 200ms.
# CAVEAT: it only affects tests with NO explicit deadline. Every property
# in test_property_tools.py sets one, so `strict` is a no-op there —
# don't read a green run under `strict` as "no timing problem".
#
# The import is guarded because several CI jobs run `pytest tests/...`
# with only `pip install pytest [pyyaml]` and no hypothesis — e.g. ci.yml's
# recipe-preview, promtool-goldens and vm-alert-parity steps,
# vm-anchor-on-pin-change.yml, nightly-vm-replay.yaml, and
# scripts/ops/federation_e2e_run.sh's venv. (Non-exhaustive: verify before
# relying on this list.) This conftest loads for those runs too, so an
# unguarded import would break their collection.
#
# The guard is deliberately narrow — it swallows ONLY "hypothesis itself
# is absent". A broken install (hypothesis present but raising ImportError
# from its own dependencies) propagates instead of silently leaving the
# stock deadline in place, which would be a fail-open.
try:
    from hypothesis import settings as _hypothesis_settings  # noqa: E402
except ModuleNotFoundError as _exc:  # pragma: no cover - env-dependent
    if _exc.name != "hypothesis":
        raise  # broken dependency, not "hypothesis not installed"
else:
    _hypothesis_settings.register_profile("vibe", deadline=None)
    _hypothesis_settings.register_profile("strict", deadline=200)
    # `or "vibe"`: an env var set-but-empty (common with `docker run -e VAR`
    # when VAR is unset on the host) must not brick collection. An unknown
    # name falls back with a warning rather than raising, because
    # load_profile() raising here aborts the ENTIRE tests/ tree with a
    # traceback pointing at conftest — and "ci"/"dev" are names a developer
    # may already export globally for an unrelated project.
    _profile = os.environ.get("HYPOTHESIS_PROFILE") or "vibe"
    if _profile not in ("vibe", "strict"):
        print(
            f"[conftest] unknown HYPOTHESIS_PROFILE={_profile!r}; "
            f"falling back to 'vibe' (valid: vibe, strict)",
            file=sys.stderr,
        )
        _profile = "vibe"
    _hypothesis_settings.load_profile(_profile)


# ── Session-scoped subprocess UTF-8 setup ────────────────────────────
#
# When tests use `subprocess.run([sys.executable, ...], capture_output=True,
# text=True, encoding='utf-8')`, the parent decodes child output as UTF-8
# but the CHILD Python's `sys.stdout.encoding` defaults to the host
# console codepage. On zh-TW Windows that's cp950 (Big5) and CJK
# characters in script output (e.g., `print("中文錯誤")`) become un-
# decodable bytes once they reach the parent's UTF-8 decoder, surfacing
# as `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa1 …`.
#
# Setting `PYTHONIOENCODING=utf-8` in the test process environment
# propagates to every spawned child Python (since `subprocess.run` without
# an explicit `env=` kwarg inherits the parent env). The child then
# emits UTF-8 bytes, matching what the parent expects.
#
# Linux CI runners default to UTF-8 anyway so this is a no-op there;
# the session scope means it runs once per pytest run, not per test.
@pytest.fixture(scope="session", autouse=True)
def _force_utf8_subprocess_io():
    """Force UTF-8 IO for subprocess'd Python children (cross-platform parity)."""
    old = os.environ.get("PYTHONIOENCODING")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("PYTHONIOENCODING", None)
        else:
            os.environ["PYTHONIOENCODING"] = old


# ── Session-scoped constant fixtures ──────────────────────────────────

@pytest.fixture(scope="session")
def metric_dictionary():
    """metric-dictionary.yaml 完整內容（session scope，僅載入一次）。"""
    import yaml
    dict_path = os.path.join(REPO_ROOT, "scripts", "tools", "metric-dictionary.yaml")
    with open(dict_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Function-scoped fixtures ──────────────────────────────────────────

@pytest.fixture
def config_dir():
    """Provide a temporary directory for config files, cleaned up after test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def routing_dir():
    """預載兩個 tenant routing YAML 的暫存目錄。

    提供 db-a（webhook）+ db-b（slack）兩個 tenant 的標準 config，
    適用於 integration test 中 scaffold → generate_routes 管線。

    Yields:
        str — 含 db-a.yaml + db-b.yaml 的暫存目錄路徑。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        populate_routing_dir(tmpdir)
        yield tmpdir


@pytest.fixture
def cli_argv(monkeypatch):
    """Replace `sys.argv` with the given args. Auto-restored after test.

    Convenience over the repeated
    `monkeypatch.setattr(sys, "argv", [...])` pattern (195 sites across
    the suite as of audit verification). Variadic API — pass the
    program name as the first positional arg, then flags + values:

        def test_x(cli_argv):
            cli_argv("script.py", "--input", str(tmp_path / "in.yaml"))
            ...

    Same semantics as monkeypatch.setattr — the fixture's monkeypatch
    dependency means the override is automatically reverted at end of
    test. Tests that ALSO patch other things keep their monkeypatch
    arg alongside cli_argv.
    """
    def _set(*args):
        monkeypatch.setattr(sys, "argv", list(args))
    return _set


@pytest.fixture
def patch_repo_root(monkeypatch, tmp_path):
    """Replace a tool module's repo-root constant with `tmp_path`.

    Returns a callable: `patch_repo_root(module, attr="REPO_ROOT") -> Path`.
    The returned Path is the same `tmp_path` so the caller can populate
    fixture data on it.

    Why this exists: 60+ tests across 13 files were repeating
    `monkeypatch.setattr(my_module, "REPO_ROOT", tmp_path)` (or `PROJECT_ROOT`,
    `CLAUDE_MD`, etc.) verbatim. The audit's TRK-242 sweep showed how easily
    one renamed constant breaks every test that hard-codes the attribute
    name; this fixture funnels them through one indirection.

    Usage:
        def test_foo(patch_repo_root):
            root = patch_repo_root(my_module)              # default REPO_ROOT
            (root / "docs").mkdir()
            ...

        def test_bar(patch_repo_root):
            root = patch_repo_root(my_module, "PROJECT_ROOT")   # explicit attr
            ...

        def test_file_path(patch_repo_root):
            root = patch_repo_root(my_module)
            claude_md = root / "CLAUDE.md"
            claude_md.write_text("...")
            patch_repo_root(my_module, "CLAUDE_MD", value=claude_md)
            ...
    """
    def _patch(module, attr: str = "REPO_ROOT", value=None):
        target = tmp_path if value is None else value
        monkeypatch.setattr(module, attr, target)
        return tmp_path
    return _patch


