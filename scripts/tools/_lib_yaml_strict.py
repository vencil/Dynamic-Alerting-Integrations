"""Strict YAML reading: a duplicate mapping key is an error (#2123).

⛔ THE one definition of "this YAML repeats a key" for every Python reader:
`_lib_input.decode_bytes` (every declared input, #4064) and `_lib_io`
(`load_yaml_file`, which decodes through it; the lints that read with
`strict_safe_load`). `_lib_io` re-exports every name here, so a caller
importing them from it is unchanged.

Its own module because `_lib_input` is standard library only and imports
`_lib_exitcodes` (the crash hook) only on a refusal, while `_lib_io`
imports it at the top: this module imports PyYAML and nothing of the repo,
and `_lib_input` imports it only when it parses YAML.
"""
from __future__ import annotations

from typing import Any, Iterator, Optional

import yaml


#
# YAML forbids a mapping from holding the same key twice. PyYAML does not
# enforce it: `safe_load` keeps the LAST value and says nothing. The
# exporter's `gopkg.in/yaml.v3` does enforce it and rejects the whole file
# (`mapping key "x" already defined at line N`), so a tool that judges a
# conf.d with `safe_load` passes a file the exporter will not serve. Not in
# every position: yaml.v3 decoding into the exporter's typed config skips a
# field it does not know, repeated keys under it included, and reads only
# the first document. That is exporter semantics (#2033 R′: not modelled);
# the error message below therefore states the YAML rule only.
#
# ⛔ This is the YAML standard, not a model of the exporter. The ONE thing
# mirrored from yaml.v3 is key identity, because the standard leaves the
# comparison to the implementation and the exporter is the reader that
# counts. yaml.v3 (`decode.go`, `mapping()`, uniqueKeys) compares two keys
# of one mapping by node kind + raw text — measured on this repo's pinned
# v3.0.1 (#2123; matrix: tests/shared/test_yaml_strict_loader.py _IDENTITY).
# So keys that arrive through a merge are not compared with the mapping's
# own keys; everything else is compared by what was written. Nothing here
# decides what a key MEANS (`true` vs `True`, `tenants: null`, scalars where
# a mapping belongs) — that is exporter semantics and out of scope (#2033).
#
# ⚠️ NOT GUARDED: an ALIAS used as a key. yaml.v3 compares the alias node
# (text = the anchor name); PyYAML's composer hands back the anchored node
# itself, so this compares the anchored text instead.
#
# The walk runs over the COMPOSED node tree of each document before anything
# is constructed, so it sees every mapping in the document — including ones
# no consumer reads — and it does not depend on which constructor runs next.


class DuplicateKeyError(yaml.constructor.ConstructorError):
    """One mapping holds the same key twice (#2123).

    A ``yaml.YAMLError`` on purpose: every ``except yaml.YAMLError`` a tool
    already has for a syntax error takes this on the same path, with the
    same message shape — PyYAML's marks name the file (when the stream is
    named), the line and the column of the SECOND occurrence.

    Attributes: ``key`` (the raw key text), ``first_line`` (1-based line of
    the first occurrence).
    """

    def __init__(self, dup: "yaml.Node", first: "yaml.Node",
                 context_mark: Any = None) -> None:
        self.key = dup.value if isinstance(dup, yaml.ScalarNode) else "<non-scalar>"
        self.first_line = first.start_mark.line + 1
        super().__init__(
            "while constructing a mapping", context_mark,
            f"found duplicate key {self.key!r} (first defined at line "
            f"{self.first_line}); YAML does not allow duplicate mapping keys",
            dup.start_mark)


def _key_identity(node: "yaml.Node") -> tuple[str, str]:
    """yaml.v3's uniqueKeys comparison: node kind + raw text (see above)."""
    return (type(node).__name__,
            node.value if isinstance(node, yaml.ScalarNode) else "")


def duplicate_in_mapping(node: "yaml.MappingNode"
                         ) -> Optional[tuple["yaml.Node", "yaml.Node"]]:
    """``(second, first)`` key nodes of the first repeated key in ONE mapping
    node's own entries, or ``None``. Keys brought in by a ``<<`` merge are
    not entries of this node, so they are never compared here."""
    seen: dict[tuple[str, str], "yaml.Node"] = {}
    for key_node, _value in node.value:
        ident = _key_identity(key_node)
        if ident in seen:
            return key_node, seen[ident]
        seen[ident] = key_node
    return None


def find_duplicate_key(root: Optional["yaml.Node"]
                       ) -> Optional[tuple["yaml.Node", "yaml.Node", "yaml.Node"]]:
    """``(second, first, mapping)`` for the first repeated key anywhere under
    *root* (a composed node), or ``None``.

    Iterative, and each node is visited once: an alias is the same node
    object as its anchor, so a document that aliases a mapping many times
    (or into itself) costs one visit, not a blow-up or a recursion.
    """
    if root is None:
        return None
    stack: list["yaml.Node"] = [root]
    visited: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        if isinstance(node, yaml.MappingNode):
            hit = duplicate_in_mapping(node)
            if hit is not None:
                return hit[0], hit[1], node
            for key_node, value_node in reversed(node.value):
                stack.append(value_node)
                stack.append(key_node)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(reversed(node.value))
    return None


def raise_on_duplicate_key(root: Optional["yaml.Node"]) -> None:
    """Raise :class:`DuplicateKeyError` if *root* holds a repeated key."""
    hit = find_duplicate_key(root)
    if hit is not None:
        dup, first, mapping = hit
        raise DuplicateKeyError(dup, first, mapping.start_mark)


class RejectDuplicateKeys:
    """Loader mixin: check the whole composed document before constructing it.

    ``construct_document`` is the one method every loading path goes
    through (``get_single_data`` and ``get_data`` both call it), so the check
    cannot be skipped by which entry point a caller picks. The PARSER is
    whatever the loader class brings — pure-Python or libyaml — so a tool
    that made the pure parser's limits part of its contract keeps them.
    """

    def construct_document(self, node):  # noqa: D102 — see class
        raise_on_duplicate_key(node)
        return super().construct_document(node)  # type: ignore[misc]


def reject_equal_constructed_keys(loader: Any, pairs: Any, mapping: "yaml.Node",
                                  deep: bool = False) -> None:
    """Also refuse two keys whose CONSTRUCTED values are equal (``true`` /
    ``True``, ``1`` / ``01``) — STRICTER than yaml.v3, which compares text.

    Not the default and not the YAML-standard check above: it exists only
    so lints that compared constructed keys before #2123 (see callers of
    reject_equal_constructed_keys) keep refusing everything they refused when their private loaders were folded
    into this module. *pairs* is the caller's choice of key/value node
    pairs — flattened or not — because each lint compared a different set,
    and changing that set would change what it refuses.

    An unhashable key is skipped: ``SafeConstructor`` raises its own
    "found unhashable key" for it right after.
    """
    seen: dict[Any, "yaml.Node"] = {}
    for key_node, _value in pairs:
        key = loader.construct_object(key_node, deep=deep)
        try:
            first = seen.get(key)
        except TypeError:
            continue
        if first is not None:
            raise DuplicateKeyError(key_node, first, mapping.start_mark)
        seen[key] = key_node


class StrictSafeLoader(RejectDuplicateKeys, yaml.SafeLoader):
    """``yaml.SafeLoader`` (pure parser) that rejects a duplicate key."""


# ⛔ The loader is DRIVEN here rather than passed as `yaml.load(..., Loader=)`:
# bandit B506 (a hard gate) name-matches only a literal `yaml.SafeLoader` /
# `yaml.CSafeLoader` and flags any subclass, while a loader built and driven
# by hand is outside its predicate altogether (tests/shared/test_sast.py §4).
# So safety is pinned by BEHAVIOUR: `tests/shared/test_yaml_strict_loader.py`
# feeds these entry points a real `!!python/object/apply` payload and requires
# it to be refused. Deleting that test leaves them unguarded.
def _strict_loader_class(loader: Optional[type]) -> type:
    if loader is None:
        return StrictSafeLoader
    # A caller-supplied class must still be one of these underneath:
    # anything else could be a loader that constructs arbitrary objects.
    if not issubclass(loader, StrictSafeLoader):
        raise TypeError(f"{loader.__name__} is not a StrictSafeLoader subclass")
    return loader


def strict_safe_load(stream: Any, *, loader: Optional[type] = None) -> Any:
    """``yaml.safe_load`` that raises :class:`DuplicateKeyError` on a repeated
    key. The pure parser, the one ``load_yaml_file`` and ``yaml.safe_load``
    use. *loader* is a :class:`StrictSafeLoader` subclass, for a caller that
    adds a constructor rule of its own on top."""
    loader = _strict_loader_class(loader)(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def strict_safe_load_all(stream: Any, *, loader: Optional[type] = None) -> Iterator[Any]:
    """``yaml.safe_load_all`` that raises :class:`DuplicateKeyError` on a
    repeated key. LAZY like the original: a caller that takes only the first
    document (``next(...)``) never parses — or checks — a later one.
    *loader* as in :func:`strict_safe_load`."""
    loader = _strict_loader_class(loader)(stream)
    try:
        while loader.check_data():
            yield loader.get_data()
    finally:
        loader.dispose()
