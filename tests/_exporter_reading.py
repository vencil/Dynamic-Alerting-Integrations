"""YAML text as the exporter reads it, for tests that check what a tool wrote
(ADR-036 §12, #3206).

"What does the exporter read from this file" has one answer: its consumer's
reading, `da-guard parse-tree` (yaml.v3). A test asks it through the shared
wrapper (`_lib_parse_cache.read_text_as`, parse-tree's file mode) and never
parses the YAML itself: mapping keys come back as their source text (the
exporter keys tenants by the scalar's text), values by their yaml.v3 tag.

*text_scalars*: mapping keys whose non-null scalar value is returned as its
source text — a tenant's `_profile: 010` names profile `010:`, as the
exporter's `ScheduledValue` keeps the text. A null value stays None.
"""
from __future__ import annotations

import tempfile
from typing import Any, Iterable

import _lib_parse_cache
import _lib_parse_tree


def _with_text_scalars(node: Any, keys: "frozenset[str]") -> Any:
    out = _lib_parse_tree.to_python(node)
    if not keys:
        return out
    stack = [(node, out)]
    while stack:
        src, dst = stack.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for k, child in items:
            if _lib_parse_tree.is_scalar(child):
                if isinstance(src, dict) and k in keys and child["tag"] != "!!null":
                    dst[k] = _lib_parse_tree.scalar_text(child)
            else:
                stack.append((child, dst[k]))
    return out


def exporter_reading(text: str, *, rel: str = "t.yaml",
                     text_scalars: Iterable[str] = ()) -> Any:
    """*text* read as the conf.d file *rel* (alone in an empty conf.d) is read
    by the exporter: parse-tree's file mode. Raises `FileUnreadable` for a
    file the exporter does not serve."""
    keys = frozenset(text_scalars)
    mode = _lib_parse_cache.ParseMode(
        f"tests:exporter_reading(text_scalars={sorted(keys)!r})",
        lambda node: _with_text_scalars(node, keys))
    with tempfile.TemporaryDirectory(prefix="da-exporter-reading-") as empty:
        return _lib_parse_cache.read_text_as(empty, rel, text.encode("utf-8"), mode)
