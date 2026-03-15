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
def rule_packs():
    """RULE_PACKS 常數（session scope，僅載入一次）。"""
    from scaffold_tenant import RULE_PACKS
    return RULE_PACKS


@pytest.fixture(scope="session")
def command_map():
    """COMMAND_MAP 常數（session scope，僅載入一次）。"""
    import entrypoint
    return entrypoint.COMMAND_MAP


@pytest.fixture(scope="session")
def guardrails():
    """GUARDRAILS timing 常數（session scope，僅載入一次）。"""
    from _lib_python import GUARDRAILS
    return GUARDRAILS


@pytest.fixture(scope="session")
def receiver_types():
    """RECEIVER_TYPES 常數（session scope，僅載入一次）。"""
    from _lib_python import RECEIVER_TYPES
    return RECEIVER_TYPES


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
