"""The site pages that live outside ``docs/`` (#4251).

``scripts/mkdocs/rule_packs_bridge.py`` copies ``RULE_PACK_DOCS`` from the repo
root's ``rule-packs/`` into ``docs/rule-packs/`` for the length of a build. They
are site pages like any under ``docs/``, so whatever judges or rewrites a site
page's front matter reads them here, at their source — never the transient,
gitignored copy, which a killed build may leave behind with stale content.

The bridge's tuple is THE list; this module only reads it, so the two cannot
disagree. Repo-only: the da-tools image does not ship ``scripts/mkdocs``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "scripts" / "mkdocs" / "rule_packs_bridge.py"

#: Where the bridge puts its copy, relative to ``docs/``: a site path, never a
#: source — a reader of ``docs/`` skips it.
BRIDGED_DOCS_SUBDIR = "rule-packs"


def bridged_pages(repo_root: Path = REPO_ROOT) -> "list[Path]":
    """The bridged pages at their source (``<repo_root>/rule-packs/<name>``),
    in the bridge's order. Raises if the bridge cannot be read: a list that
    silently came back empty would judge nothing."""
    spec = importlib.util.spec_from_file_location("_rule_packs_bridge", BRIDGE)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {BRIDGE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = tuple(module.RULE_PACK_DOCS)
    if not names:
        raise ValueError(f"{BRIDGE}: RULE_PACK_DOCS is empty")
    return [Path(repo_root) / "rule-packs" / name for name in names]
