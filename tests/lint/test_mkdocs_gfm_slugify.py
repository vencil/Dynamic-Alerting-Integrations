"""The site's heading ids follow github.com's rule (#1375, "B3").

`scripts/mkdocs/gfm_slugify.py` points mkdocs' `toc.slugify` at the same
`_gfm_slug` that `check_doc_links.py` enforces. These tests pin the two halves
that make the site and GitHub agree:

1. Rendering a heading through Python-Markdown's `toc` with the hook installed
   yields the id `_GfmSlugger` computes — CJK kept, one `-` per space, GitHub's
   `-1` duplicate suffix (not toc's `_1`).
2. No doc line outside a code fence starts with a `#` that is not followed by a
   space. CommonMark reads `#539 Phase 2` as text; Python-Markdown reads it as an
   `<h1>`, so the site showed a sentence as a page-sized heading with an id
   GitHub never produces.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import markdown
import markdown.extensions.toc as toc_ext
import pytest

from _tree import REPO_ROOT, repo_files

sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "lint"))
from check_doc_links import _GfmSlugger  # noqa: E402

_HOOK = REPO_ROOT / "scripts" / "mkdocs" / "gfm_slugify.py"

HEADINGS = [
    "設計概念總覽",
    "附錄：角色與工具速查",
    "1.1 平台規則：為什麼與租戶數無關（O(M)）",
    "v2.10.0 Lessons Learned — Mutation harness + 宣稱紀律（2026-08-09, PR #1370）",
    "Appendix A: Role & Tool Quick Reference",
    "`_defaults.yaml` 繼承語意",
    "Echo",
    "Echo",
    "Echo",
]


@pytest.fixture
def hook(monkeypatch):
    spec = importlib.util.spec_from_file_location("gfm_slugify_hook", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(toc_ext, "unique", toc_ext.unique)  # restored after the test
    config = module.on_config({"mdx_configs": {}})
    return config["mdx_configs"]["toc"]


def test_rendered_ids_match_github(hook):
    md = markdown.Markdown(extensions=["toc"], extension_configs={"toc": hook})
    html = md.convert("\n\n".join(f"## {h}" for h in HEADINGS))
    site_ids = re.findall(r'<h2 id="([^"]*)"', html)

    slugger = _GfmSlugger()
    github_ids = [slugger.slug(h) for h in HEADINGS]

    assert site_ids == github_ids
    assert github_ids[-3:] == ["echo", "echo-1", "echo-2"]  # non-vacuity: duplicates exercised


def test_default_toc_disagrees_without_the_hook():
    # Counterfactual: without the hook the same heading loses its CJK, which is
    # the divergence this hook exists to remove.
    html = markdown.Markdown(extensions=["toc"]).convert("## 附錄：角色與工具速查")
    assert 'id="附錄角色與工具速查"' not in html


_FENCE = re.compile(r"\s*(`{3,}|~{3,})")
_LAX_ATX = re.compile(r"\s*(?:[-*+]\s+|\d+\.\s+|>\s*)*#{1,6}[^#\s\\]")


def _lax_atx_lines() -> list[str]:
    hits = []
    for path in repo_files(".md"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith("docs/"):
            continue
        fence = None
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = _FENCE.match(line)
            if m:
                marker = m.group(1)
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
                continue
            if fence is None and _LAX_ATX.match(line):
                hits.append(f"{rel}:{n}: {line.strip()[:80]}")
    return hits


def test_no_line_the_site_would_render_as_a_heading_but_github_would_not():
    hits = _lax_atx_lines()
    assert not hits, (
        "these lines start with `#` + non-space; the site renders them as headings, "
        "GitHub as text. Escape the first `#` (`\\#539`):\n  " + "\n  ".join(hits)
    )


def test_lax_atx_pattern_matches_what_it_claims():
    # Non-vacuity for the scan above.
    assert _LAX_ATX.match("#539 Phase 2")
    assert _LAX_ATX.match("- #5: foo")
    assert _LAX_ATX.match("> #1339): bar")
    assert not _LAX_ATX.match("## Real heading")
    assert not _LAX_ATX.match("\\#539 escaped")
