"""The mkdocs bridge leaves no copy behind, whether the build succeeds or not (#4251).

``scripts/mkdocs/rule_packs_bridge.py`` copies the repo root's rule-pack pages
into ``docs/rule-packs/`` for the length of a build. A copy left after a failed
build is gitignored, so ``git status`` does not show it, and readers of
``docs/`` took its stale front matter for the repo's.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import _lib_site_pages  # noqa: E402


def _bridge():
    spec = importlib.util.spec_from_file_location("bridge_under_test", _lib_site_pages.BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path):
    (tmp_path / "rule-packs").mkdir()
    for name in _bridge().RULE_PACK_DOCS:
        (tmp_path / "rule-packs" / name).write_text(f"---\nversion: v9.9.9\n---\n# {name}\n",
                                                    encoding="utf-8")
    return {"config_file_path": str(tmp_path / "mkdocs.yml")}


def test_a_failed_build_leaves_no_copy(tmp_path):
    bridge = _bridge()
    config = _tree(tmp_path)
    bridge.on_pre_build(config)
    assert sorted(p.name for p in (tmp_path / "docs" / "rule-packs").iterdir()) == sorted(bridge.RULE_PACK_DOCS)
    bridge.on_build_error(error=RuntimeError("snippet missing"))
    assert not (tmp_path / "docs" / "rule-packs").exists()


def test_a_successful_build_leaves_no_copy(tmp_path):
    bridge = _bridge()
    config = _tree(tmp_path)
    bridge.on_pre_build(config)
    bridge.on_post_build(config)
    assert not (tmp_path / "docs" / "rule-packs").exists()


def test_a_copy_a_killed_build_left_is_not_served(tmp_path):
    """A build killed before either event leaves its copy; the next build
    starts from the source, not from what is on disk."""
    bridge = _bridge()
    config = _tree(tmp_path)
    stale = tmp_path / "docs" / "rule-packs"
    stale.mkdir(parents=True)
    (stale / "README.md").write_text("---\nversion: v1.12.0\n---\n", encoding="utf-8")
    (stale / "left-behind.md").write_text("stale\n", encoding="utf-8")
    bridge.on_pre_build(config)
    assert not (stale / "left-behind.md").exists()
    assert "v9.9.9" in (stale / "README.md").read_text(encoding="utf-8")


def test_the_site_page_list_is_the_bridges():
    assert [p.name for p in _lib_site_pages.bridged_pages()] == list(_bridge().RULE_PACK_DOCS)
    assert all(p.is_file() for p in _lib_site_pages.bridged_pages())
