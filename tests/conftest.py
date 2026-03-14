"""Shared test fixtures, helpers, and sys.path setup for da-tools test suite.

This conftest.py is loaded automatically by pytest before any test module.
It sets up sys.path so every test can ``import scaffold_tenant``, ``import
_lib_python``, ``import bump_docs``, etc. without per-file boilerplate.
"""
import os
import stat
import sys

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


# ── Shared helpers ───────────────────────────────────────────────────

def write_yaml(tmpdir, filename, content):
    """Write a YAML file into tmpdir with secure permissions.

    Returns the absolute path to the written file.
    """
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path
