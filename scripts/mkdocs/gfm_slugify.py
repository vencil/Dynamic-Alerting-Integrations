"""mkdocs hook — give the site the same heading anchors github.com gives (#1375, "B3").

Python-Markdown's default `toc` slugify is ASCII-only: it deletes CJK outright
and collapses whitespace runs, while github.com keeps CJK and turns every space
into one `-`. So a link such as `foo.md#3-設計原則` worked on GitHub and 404'd on
the site (or the reverse), and `scripts/tools/lint/mkdocs-anchor-debt.txt` had to
carry every such pair as known debt.

This hook makes the site use the SAME slug function `check_doc_links.py` already
enforces in CI (`_gfm_slug`) — loaded from that file, not copied, so the two
gates cannot drift apart again. Duplicate headings on a page get GitHub's `-1`,
`-2` suffixes instead of Python-Markdown's `_1`, `_2` (`toc.unique` is replaced
for the build process; nothing else in a mkdocs build calls it).

⚠️ This changes anchor values on the published site: every CJK heading's id is
different from before. Inbound deep links written against the old ASCII-only ids
break — the trade accepted in #1375, since the in-repo links were written for
GitHub's rule all along.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import markdown.extensions.toc as _toc

_CHECKER = Path(__file__).resolve().parents[1] / "tools" / "lint" / "check_doc_links.py"


def _load_gfm_slug():
    spec = importlib.util.spec_from_file_location("_check_doc_links_for_mkdocs", _CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._gfm_slug


_gfm_slug = _load_gfm_slug()


def gfm_slugify(value: str, separator: str) -> str:
    """`toc` calls this with the heading's rendered text; `separator` is ignored."""
    return _gfm_slug(value)


def gfm_unique(id: str, ids: set) -> str:  # noqa: A002 - mirrors toc.unique's signature
    """GitHub's duplicate suffix: `x`, `x-1`, `x-2`, … (toc's own is `x_1`)."""
    if not id:
        # An empty slug (a heading of only emoji/punctuation) is not a usable id
        # attribute; keep toc's behaviour for it rather than emit id="".
        return _ORIGINAL_UNIQUE(id, ids)
    result, n = id, 0
    while result in ids:
        n += 1
        result = f"{id}-{n}"
    ids.add(result)
    return result


_ORIGINAL_UNIQUE = _toc.unique


def on_config(config):
    config["mdx_configs"].setdefault("toc", {})["slugify"] = gfm_slugify
    _toc.unique = gfm_unique
    return config
