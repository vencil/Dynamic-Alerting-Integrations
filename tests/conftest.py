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
    `CLAUDE_MD`, etc.) verbatim. The audit's TD-042 sweep showed how easily
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


# ── pytest configuration ──────────────────────────────────────────────

def pytest_addoption(parser):
    """Register custom command-line options."""
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="Update snapshot baselines"
    )
