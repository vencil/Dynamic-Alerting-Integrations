"""File I/O and YAML helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import functools
import io
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Optional, TypeVar

import yaml

from _lib_constants import ONBOARD_HINTS_FILENAME
# Leaf module (yaml + stdlib only), so no import cycle (#2114).
from _lib_yaml_keys import ExporterKeyLoader, load_exporter_keys
# No import cycle: _lib_exitcodes imports only stdlib + _lib_compat (#1641).
import _lib_exitcodes  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, die_caller_error

_F = TypeVar("_F", bound=Callable[..., Any])


class YamlFileError(yaml.YAMLError):
    """A YAML file that exists but cannot be read — and WHICH file (#1654).

    ``yaml.YAMLError`` subclass on purpose: every ``except yaml.YAMLError``
    (and ``except Exception``) a caller already has catches this, so a
    decode failure now takes the same message path as a syntax error
    instead of escaping as a traceback. Attributes:

    * ``path``  — the file as the caller named it.
    * ``cause`` — the original error (also ``__cause__``): the
      ``UnicodeDecodeError`` for content that is not valid UTF-8, the
      PyYAML error for bad syntax.

    ``str()`` is ONE line, ``<path>: <original message> (<CauseClass>)``,
    with the original's line breaks collapsed so the line/column PyYAML
    reports survive but a caller can interpolate it into a report line.
    """

    def __init__(self, path: str, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        detail = " ".join(str(cause).split()) or cause.__class__.__name__
        super().__init__(f"{path}: {detail} ({cause.__class__.__name__})")


# libyaml's C parser when the wheel ships it (every PyPI wheel does), the
# pure-Python one otherwise. Same document model, same error classes and
# marks (a named stream still names the file); only the wording of a few
# parser messages differs ("did not find expected" vs "expected … but got").
# Measured on the tracked corpus: 422 files parse to identical objects, 11.7x
# faster; rule-pack-heavy tools halve their wall time (#1910 line, PR-7).
#
# ⛔ OPT-IN, per tool. `load_yaml_file()` below deliberately stays on
# `yaml.safe_load` (the pure parser): the two parsers differ on MALFORMED
# input — libyaml accepts a trailing tab the pure parser rejects, and parses
# nesting the pure parser dies on with RecursionError — and tools that read
# operator files have made the pure parser's limits part of their contract
# (`deprecate_rule` names "本工具讀不了（pure parser 限制）" as a distinct
# verdict, `validate_config` / `_grar_parse` route RecursionError as
# "unreadable file, exit 1"). Switch a tool here only when it reads
# repository-tracked YAML and no test pins its behaviour on a bad file.
# `tests/shared/test_yaml_loader_is_libyaml.py` pins both the opt-in and the
# boundary.
SAFE_LOADER = (yaml.CSafeLoader if getattr(yaml, "__with_libyaml__", False)
               else yaml.SafeLoader)


if getattr(yaml, "__with_libyaml__", False):
    def safe_load(stream: Any) -> Any:
        """`yaml.safe_load` on libyaml's C parser (see SAFE_LOADER above).

        Two literal branches rather than `Loader=SAFE_LOADER`: bandit B506
        (a hard gate here, run with --ignore-nosec) only recognises the
        loader when it is spelled `yaml.CSafeLoader` / `yaml.SafeLoader` at
        the call, and this is the one place in the tool family that spells it.
        """
        return yaml.load(stream, Loader=yaml.CSafeLoader)
else:
    def safe_load(stream: Any) -> Any:
        """Pure-Python fallback when PyYAML was built without libyaml."""
        return yaml.load(stream, Loader=yaml.SafeLoader)


def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.

    Raises:
        YamlFileError: the file exists but cannot be read — content that is
            not valid UTF-8 (``cause`` is the ``UnicodeDecodeError``) OR bad
            YAML syntax (``cause`` is the PyYAML error). Before #1654 the
            first escaped as a bare ``UnicodeDecodeError`` — a ``ValueError``
            no ``except yaml.YAMLError`` in this repo saw, and one that never
            carried the path. ⚠️ Never swallowed into *default*: a file that
            is present but unreadable is the loudest input, not an empty one.
        OSError: as before; not wrapped.

    Decoding is STRICT UTF-8, exactly what the text-mode ``open`` did, so
    the accepted encodings are unchanged: this helper must not start
    serving (say) UTF-16 while ``validate_config``'s own reads and the
    routes generator still refuse it — that would be #1911's "one input,
    two answers" inside the Python tool family (blind review). Whether the
    family should follow the exporter's parser on other encodings is a
    separate decision. The decoded text is parsed from a named stream so
    PyYAML's own marks (``in "<path>", line N, column M``) keep naming the
    real file instead of ``"<unicode string>"``.
    """
    if not path or not Path(path).is_file():
        return default
    raw = Path(path).read_bytes()
    try:
        stream = io.StringIO(raw.decode("utf-8"))
        stream.name = str(path)
        data = yaml.safe_load(stream)   # deliberately the pure parser: see safe_load()
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise YamlFileError(str(path), exc) from exc
    return data if data is not None else default


def load_yaml_file_exporter_keys(
    path: Optional[str],
    default: Any = None,
    *,
    raw_text_sequences: "tuple[str, ...] | frozenset[str]" = (),
    raw_text_scalars: "tuple[str, ...] | frozenset[str]" = (),
) -> Any:
    """:func:`load_yaml_file`, except every mapping KEY is the scalar's raw
    TEXT — the tenant id the exporter reads (#2114; ``_lib_yaml_keys``).

    Same contract as :func:`load_yaml_file` in every other respect — *default*
    for a missing / empty file, :class:`YamlFileError` naming the file for
    non-UTF-8 content or bad syntax, and the SAME pure-Python parser, so a
    file this refuses is exactly a file :func:`load_yaml_file` refuses (its
    callers route those limits, see ``SAFE_LOADER``). Values stay
    PyYAML-typed; *raw_text_sequences* names the keys whose list value is a
    list of tenant ids and comes back as source text too; *raw_text_scalars*
    names the keys whose scalar value names a key (``_profile``, #2216).

    ⚠️ NOT strict: a duplicate key keeps the last value, like
    :func:`load_yaml_file`. A reader that must refuse a duplicate key reads
    the text with :func:`strict_load_exporter_keys` (#2123 decided which
    readers are strict); a conf.d reader goes through da-guard parse-tree
    instead (ADR-036).

    ⚠️ A sibling rather than a flag on :func:`load_yaml_file`: that helper
    has dozens of callers reading files that hold no tenant id, and changing
    their key types would be a change nobody asked them about.
    """
    if not path:
        return default
    file = Path(path)
    if not file.is_file():
        return default
    try:
        stream = io.StringIO(file.read_bytes().decode("utf-8"))
        stream.name = str(path)   # PyYAML marks keep naming the real file
        data = load_exporter_keys(stream,
                                  raw_text_sequences=raw_text_sequences,
                                  raw_text_scalars=raw_text_scalars)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise YamlFileError(str(path), exc) from exc
    return default if data is None else data


# ── Strict reading: a duplicate mapping key is an error (#2123) ─────────────
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


if getattr(yaml, "__with_libyaml__", False):
    class StrictCSafeLoader(RejectDuplicateKeys, yaml.CSafeLoader):
        """``yaml.CSafeLoader`` (libyaml) that rejects a duplicate key."""
else:  # pragma: no cover - every PyPI wheel ships libyaml
    StrictCSafeLoader = StrictSafeLoader  # type: ignore[misc,assignment]


class StrictExporterKeyLoader(RejectDuplicateKeys, ExporterKeyLoader):
    """Strict (#2123) AND exporter keys (#2114): the two compose.

    ``RejectDuplicateKeys`` checks the composed node tree before anything is
    constructed, by ``_key_identity`` — kind + raw text, which is the
    exporter's identity too — so ``123:`` and ``"123":`` in one mapping are
    ONE key and raise :class:`DuplicateKeyError`, as yaml.v3 rejects them
    (``mapping key "123" already defined``). ``ExporterKeyLoader`` then
    builds the keys as that same raw text. Pure-Python parser underneath
    (``ExporterKeyLoader`` is a ``yaml.SafeLoader``), like every reader that
    uses it had before.
    """


@functools.lru_cache(maxsize=None)
def _strict_exporter_key_class(raw_text_sequences: "frozenset[str]",
                               raw_text_scalars: "frozenset[str]" = frozenset()
                               ) -> type:
    """``StrictExporterKeyLoader``, or a subclass that also reads the lists
    under *raw_text_sequences* / the non-null scalars under
    *raw_text_scalars* as source text (``ExporterKeyLoader``'s attributes of
    the same names) — a class, because the strict entry points below build
    the loader themselves. Both sets are part of the cache key."""
    if not raw_text_sequences and not raw_text_scalars:
        return StrictExporterKeyLoader
    return type("StrictExporterKeyLoader_raw", (StrictExporterKeyLoader,),
                {"raw_text_sequences": raw_text_sequences,
                 "raw_text_scalars": raw_text_scalars})


# ⛔ The loader is DRIVEN here rather than passed as `yaml.load(..., Loader=)`:
# bandit B506 (a hard gate) name-matches only a literal `yaml.SafeLoader` /
# `yaml.CSafeLoader` and flags any subclass, while a loader built and driven
# by hand is outside its predicate altogether (tests/shared/test_sast.py §4).
# So safety is pinned by BEHAVIOUR: `tests/shared/test_yaml_strict_loader.py`
# feeds these entry points a real `!!python/object/apply` payload and requires
# it to be refused. Deleting that test leaves them unguarded.
def _strict_loader_class(fast: bool, loader: Optional[type]) -> type:
    if loader is None:
        return StrictCSafeLoader if fast else StrictSafeLoader
    # A caller-supplied class must still be one of these underneath:
    # anything else could be a loader that constructs arbitrary objects.
    if not issubclass(loader, (StrictSafeLoader, StrictCSafeLoader,
                               StrictExporterKeyLoader)):
        raise TypeError(f"{loader.__name__} is not a StrictSafeLoader / "
                        f"StrictCSafeLoader / StrictExporterKeyLoader subclass")
    return loader


def strict_safe_load(stream: Any, *, fast: bool = False,
                     loader: Optional[type] = None) -> Any:
    """``yaml.safe_load`` that raises :class:`DuplicateKeyError` on a repeated
    key. ``fast=True`` uses libyaml (see ``SAFE_LOADER``); the default is the
    pure parser, the one ``load_yaml_file`` and ``yaml.safe_load`` use.
    *loader* is a subclass of one of the two strict loaders, for a caller
    that adds a constructor rule of its own on top."""
    loader = _strict_loader_class(fast, loader)(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def strict_safe_load_all(stream: Any, *, fast: bool = False,
                         loader: Optional[type] = None) -> Iterator[Any]:
    """``yaml.safe_load_all`` that raises :class:`DuplicateKeyError` on a
    repeated key. LAZY like the original: a caller that takes only the first
    document (``next(...)``) never parses — or checks — a later one.
    *fast* / *loader* as in :func:`strict_safe_load`."""
    loader = _strict_loader_class(fast, loader)(stream)
    try:
        while loader.check_data():
            yield loader.get_data()
    finally:
        loader.dispose()


def load_yaml_file_strict(path: Optional[str], default: Any = None) -> Any:
    """:func:`load_yaml_file`, but a duplicate key is an error (#2123).

    Same contract in every other respect — same pure parser, same strict
    UTF-8 decode, same *default* for a missing or empty file — and the
    duplicate key raises the same :class:`YamlFileError` a syntax error
    does (``cause`` is the :class:`DuplicateKeyError`), so a caller's
    existing "cannot read this file" path reports it with no new branch.
    """
    # Spelled differently from load_yaml_file on purpose: the mutation
    # catalogue anchors on that function's lines being unique in the file.
    if not (path and Path(path).is_file()):
        return default
    raw = Path(path).read_bytes()
    try:
        stream = io.StringIO(raw.decode("utf-8"))
        stream.name = str(path)
        data = strict_safe_load(stream)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise YamlFileError(str(path), exc) from exc
    return default if data is None else data


def load_confd_file(config_dir: "str | os.PathLike[str]", path: Optional[str],
                    default: Any = None, *, mode: Any = None, reader: Any = None) -> Any:
    """A conf.d file's content as `da-guard parse-tree` reads it (ADR-036
    step 4, #2906) — the by-path read of the routing tools, which do not
    parse conf.d with PyYAML. *config_dir* is the conf.d root *path* belongs
    to (the session's reader of it, `_lib_parse_cache.reader_for`); *mode* a
    `_lib_parse_cache.ParseMode` (default VALUES); *reader* a
    `_lib_parse_cache.ConfdReader` of *config_dir* to read through instead
    (one built with an explicit da-guard binary).

    *default* for a path that is no config file of the tree — one the
    reader's listing does not hold (an absent `_routing_profiles.yaml`), or
    one Go's `role` says is no file (a directory, a dangling link). ⛔ Not
    `is_file()` (#3206): it raised for a path under a directory this uid may
    not search, and answered False for a file it may not stat; a config file
    whose read fails raises. A file parse-tree cannot read raises
    :class:`YamlFileError` (``cause`` is the `_lib_parse_tree.FileUnreadable`,
    whose ``kind`` names why), so a caller's "cannot read this file" path
    reports it with no new branch. An empty file is ``{}``. No usable
    da-guard raises `_lib_parse_tree.ParseTreeError`, never a default."""
    if not path:
        return default
    import _lib_parse_cache  # lazy: it runs da-guard through modules importing this one
    from _lib_parse_tree import FileUnreadable
    reader = reader or _lib_parse_cache.reader_for(config_dir)
    entry = reader.entry(path)
    if entry is None or not entry.config_file:
        return default
    try:
        return reader.read(
            path, mode if mode is not None else _lib_parse_cache.VALUES)
    except FileUnreadable as exc:
        raise YamlFileError(str(path), exc) from exc


def strict_load_exporter_keys(stream: Any, *,
                              raw_text_sequences: "frozenset[str] | tuple[str, ...]" = (),
                              raw_text_scalars: "frozenset[str] | tuple[str, ...]" = ()
                              ) -> Any:
    """:func:`strict_safe_load` whose mapping keys are the exporter's tenant
    ids (raw text, #2114). Same driver, same errors — only the loader class
    differs (:class:`StrictExporterKeyLoader`). *raw_text_scalars* names the
    keys whose non-null scalar value comes back as source text too — a value
    that names a key (``_profile: 010``, #2216/#2237); a null stays None."""
    return strict_safe_load(stream, loader=_strict_exporter_key_class(
        frozenset(raw_text_sequences), frozenset(raw_text_scalars)))


# ── #2164 → ADR-036 step 4 (#2906): a scalar in a STRING field that yaml.v3
# reads as something else
#
# Every reader of conf.d reads it the way yaml.v3 does (`da-guard
# parse-tree`), so the value a field holds is the one parse-tree's tag says:
# `channel: 1e3` is the float 1000 and `name: 2024-01-02` a timestamp, while
# `channel: yes` is the string "yes" (YAML 1.1's boolean words are strings to
# yaml.v3). The author is told to quote such a value — the #1017
# "STRING-ONLY, always quote it" rule applied to every string-typed field.
#
# The judgement reads a file's parse-tree nodes (`{text, tag, value}`
# scalars, aliases and `<<` merges already expanded). ⛔ Two things are
# deliberately NOT written down here:
#
# * WHICH texts are not strings. That is yaml.v3's verdict, parse-tree's
#   `tag`; no word list here.
# * WHICH fields are strings. That is read from the JSON Schema passed in,
#   so a field that becomes a string (or stops being one) there changes what
#   this reports with no edit here.
#
# parse-tree does not say whether a scalar was quoted, nor where it sits in
# the file: a finding names the value's path (`/tenants/t1/_routing/…`), and
# it is made the same for `yes` and `"yes"` — both are the string "yes".
#
# A field counts as a string field when, among the schema branches that can
# describe it, every one that constrains a SCALAR allows only `string` (plus
# `null`). A mixed field such as `string | number` is left alone: a bare
# number is a legal value there, and #1017's answer to that ambiguity was to
# tighten the schema to `type: string`, not to have a lint guess the intent.
_JSON_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})
_SCHEMA_COMBINATORS = ("allOf", "anyOf", "oneOf")


def _is_tree_scalar(node: Any) -> bool:
    """A parse-tree scalar (`_lib_parse_tree.is_scalar`; repeated here so this
    module needs no import of that one)."""
    return isinstance(node, dict) and isinstance(node.get("text"), str) and "tag" in node


def _written(path: str, value: str) -> str:
    """How the suggested line reads: `field: value`, or `- value` for a
    sequence item (no key to repeat)."""
    field = path.rsplit("/", 1)[-1]
    return f"- {value}" if field.isdigit() or not field else f"{field}: {value}"


class MisreadScalar:
    """One scalar in a string field that yaml.v3 reads as non-string.

    ``path`` is a JSON-pointer-like ``/tenants/t1/_routing/receiver/channel``;
    ``text`` the scalar's source text; ``resolved`` the short name of its
    yaml.v3 tag (``int``, ``float``, ``bool``, ``null``, ``timestamp``)."""

    __slots__ = ("path", "text", "resolved")

    def __init__(self, path: str, text: str, resolved: str) -> None:
        self.path, self.text, self.resolved = path, text, resolved

    def message(self) -> str:
        if self.resolved == "null":
            # ⛔ No "quote it" here: `"~"` would be a literal tilde, which is
            # almost never what an empty value meant.
            return (f"{self.path}: {self.text or '(empty)'!s} is YAML null — "
                    f"no value — but the schema requires a string for this "
                    f"field: write the value, quoted, or remove the key")
        return (f"{self.path}: {self.text!r} is read by yaml.v3 (every reader of "
                f"conf.d) as YAML {self.resolved}, not as the string the schema "
                f"requires for this field — quote it, without a tag: "
                f"{_written(self.path, chr(34) + self.text + chr(34))}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MisreadScalar({self.path} {self.text!r})"


def _schema_pointer(doc: Any, fragment: str) -> Any:
    node = doc
    for part in [p for p in fragment.split("/") if p]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _schema_expand(schema: Any, doc: str, schemas: dict[str, Any],
                   depth: int = 0) -> list[tuple[dict, str]]:
    """*schema* plus every branch it can stand for ($ref, allOf/anyOf/oneOf),
    each paired with the name of the schema document it lives in (so a $ref
    inside a referenced document resolves against THAT document).

    A cross-document ``$ref`` (``tenant-config.schema.json#/…``) is looked up
    by basename in *schemas*; an unknown one raises ``KeyError`` rather than
    silently checking less."""
    if not isinstance(schema, dict) or depth > 32:
        return []
    out = [(schema, doc)]
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target, _, fragment = ref.partition("#")
        tdoc = os.path.basename(target) if target else doc
        if tdoc not in schemas:
            raise KeyError(f"$ref {ref!r}: schema document {tdoc!r} not loaded")
        out.extend(_schema_expand(_schema_pointer(schemas[tdoc], fragment),
                                  tdoc, schemas, depth + 1))
    for kw in _SCHEMA_COMBINATORS:
        for sub in schema.get(kw) or ():
            out.extend(_schema_expand(sub, doc, schemas, depth + 1))
    return out


def _schema_key_children(cands: list[tuple[dict, str]], key: str,
                         schemas: dict[str, Any]) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    for schema, doc in cands:
        props = schema.get("properties")
        if isinstance(props, dict) and key in props:
            out.extend(_schema_expand(props[key], doc, schemas))
            continue
        matched = False
        for pattern, sub in (schema.get("patternProperties") or {}).items():
            if re.search(pattern, key):
                matched = True
                out.extend(_schema_expand(sub, doc, schemas))
        extra = schema.get("additionalProperties")
        if not matched and isinstance(extra, dict):
            out.extend(_schema_expand(extra, doc, schemas))
    return out


def _schema_item_children(cands: list[tuple[dict, str]], index: int,
                          schemas: dict[str, Any]) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    for schema, doc in cands:
        items = schema.get("items")
        if isinstance(items, dict):
            out.extend(_schema_expand(items, doc, schemas))
        elif isinstance(items, list):
            if index < len(items):
                out.extend(_schema_expand(items[index], doc, schemas))
            elif isinstance(schema.get("additionalItems"), dict):
                out.extend(_schema_expand(schema["additionalItems"], doc, schemas))
    return out


def _json_type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else "object"


def schema_scalar_types(cands: list[tuple[dict, str]]) -> Optional[frozenset]:
    """The JSON scalar types the branches in *cands* allow, or ``None`` when
    no branch constrains the value at all. Non-scalar types are dropped: a
    scalar can only ever be judged by the scalar branches."""
    types: set[str] = set()
    constrained = False
    for schema, _doc in cands:
        if "type" in schema:
            raw = schema["type"]
            types.update([raw] if isinstance(raw, str) else raw)
        elif "const" in schema:
            types.add(_json_type_of(schema["const"]))
        elif isinstance(schema.get("enum"), list):
            types.update(_json_type_of(v) for v in schema["enum"])
        else:
            continue
        constrained = True
    return frozenset(types & _JSON_SCALAR_TYPES) if constrained else None


def _tree_scalars_with_schema(root: Any, schema: dict,
                              schemas: Optional[dict[str, Any]],
                              schema_name: str) -> Iterator[tuple[dict, list, str]]:
    """Each parse-tree scalar under *root* (one file's nodes), with the schema
    branches that can describe its position and its JSON-pointer-like path,
    in document order (parse-tree's: keys byte-sorted)."""
    if root is None:
        return
    schemas = dict(schemas or {})
    schemas.setdefault(schema_name, schema)
    stack: list[tuple[Any, list[tuple[dict, str]], str]] = [
        (root, _schema_expand(schema, schema_name, schemas), "")]
    while stack:
        node, cands, path = stack.pop()
        if not cands:
            continue
        if _is_tree_scalar(node):
            yield node, cands, path
        elif isinstance(node, dict):
            for key in reversed(list(node)):
                stack.append((node[key], _schema_key_children(cands, key, schemas),
                              f"{path}/{key}"))
        elif isinstance(node, list):
            for idx in reversed(range(len(node))):
                stack.append((node[idx], _schema_item_children(cands, idx, schemas),
                              f"{path}/{idx}"))


def _walk_misread_scalars(root: Any, schema: dict, schemas: Optional[dict[str, Any]],
                          schema_name: str) -> list[tuple[dict, MisreadScalar]]:
    found: list[tuple[dict, MisreadScalar]] = []
    for node, cands, path in _tree_scalars_with_schema(root, schema, schemas, schema_name):
        if node["tag"] == "!!str":
            continue
        types = schema_scalar_types(cands)
        if types is None or types - {"null"} != {"string"}:
            continue
        if node["tag"] == "!!null" and "null" in types:
            continue
        found.append((node, MisreadScalar(path or "/", node["text"],
                                          node["tag"].lstrip("!"))))
    return found


def find_misread_scalars(root: Any, schema: dict,
                         schemas: Optional[dict[str, Any]] = None,
                         schema_name: str = "") -> list[MisreadScalar]:
    """Every scalar of *root* (a file's `da-guard parse-tree` nodes) that sits
    in a string-typed field of *schema* and that yaml.v3 read as something
    else (its tag is not `!!str`).

    *schemas* maps a schema document's basename to its parsed JSON, for
    cross-document ``$ref``; *schema_name* is *schema*'s own basename in it.
    See the block comment above for what is (and is not) a string field.
    """
    return [hit for _node, hit in _walk_misread_scalars(root, schema, schemas, schema_name)]


def misread_scalar_nodes(root: Any, schema: dict,
                         schemas: Optional[dict[str, Any]] = None,
                         schema_name: str = "") -> list[dict]:
    """The scalar NODES :func:`find_misread_scalars` reports — same walk, same
    verdict (#2695): a caller that reads such a value as the quoted string it
    was meant to be acts on the node, not on a path (a key holding ``/``
    makes two paths look nested)."""
    return [node for node, _hit in _walk_misread_scalars(root, schema, schemas, schema_name)]


# ── #2509 → #2906: a YAML 1.1 boolean word in a field that takes a boolean
#
# `send_resolved: on` / `_routing_enforced: {enabled: yes}` is the STRING
# "on" / "yes" to yaml.v3, quoted or not, and so to every reader of conf.d:
# a boolean field refuses it (the schema's `yamlBool` takes booleans only).
# The WARN says what to write instead. The words are YAML 1.1's (PyYAML's
# resolver) minus yaml.v3's six — `_lib_validation._YAML_BOOL_LITERALS`.
_YAML12_BOOL_TEXTS = frozenset(("true", "True", "TRUE", "false", "False", "FALSE"))


class YamlBoolWord:
    """One YAML 1.1 boolean word (a string to yaml.v3) in a boolean field.
    ``path`` as for :class:`MisreadScalar`; ``text`` the word as written."""

    __slots__ = ("path", "text")

    def __init__(self, path: str, text: str) -> None:
        self.path, self.text = path, text

    def message(self) -> str:
        value = "true" if self.text.lower() in ("yes", "on") else "false"
        return (f"{self.path}: {self.text!r} is the YAML 1.1 boolean word, which "
                f"yaml.v3 (every reader of conf.d) reads as the string "
                f"{self.text!r}, quoted or not — this field takes a boolean: "
                f"write {_written(self.path, value)}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"YamlBoolWord({self.path} {self.text!r})"


def find_yaml11_bool_words(root: Any, schema: dict,
                           schemas: Optional[dict[str, Any]] = None,
                           schema_name: str = "") -> list[YamlBoolWord]:
    """Every `!!str` scalar of *root* (parse-tree nodes) in a field of *schema*
    that takes a boolean, whose text is a YAML 1.1 boolean word yaml.v3 does
    not read as a boolean (`yes` / `no` / `on` / `off`, three spellings
    each) — #2509. Arguments as for :func:`find_misread_scalars`."""
    from _lib_validation import _YAML_BOOL_LITERALS
    words = frozenset(_YAML_BOOL_LITERALS) - _YAML12_BOOL_TEXTS
    found: list[YamlBoolWord] = []
    for node, cands, path in _tree_scalars_with_schema(root, schema, schemas, schema_name):
        if node["tag"] != "!!str" or node["text"] not in words:
            continue
        types = schema_scalar_types(cands)
        if types is None or "boolean" not in types:
            continue
        found.append(YamlBoolWord(path or "/", node["text"]))
    return found


def exit_on_yaml_file_error(fn: _F) -> _F:
    """Decorate a CLI ``main`` so an unreadable YAML input exits 2, named.

    The shared answer for every tool whose ``load_yaml_file`` call sites
    have no handler of their own (#1654): instead of 21 hand-written
    ``try/except`` blocks, the entry point is wrapped once, and
    :class:`YamlFileError` becomes ``ERROR: cannot read <path>: <reason>``
    on stderr plus ``sys.exit(EXIT_CALLER_ERROR)`` — an IO/decode failure
    is a caller error in ``_lib_exitcodes``, not the rc 1 an uncaught
    traceback produced.

    ⚠️ Only :class:`YamlFileError`. A bare ``yaml.YAMLError`` has no path to
    name and did not come from the helper; it keeps propagating so the
    layer that raised it stays visible.

    ``--json`` (#3115): the exit goes through ``die_caller_error`` with
    ``reason="yaml_file_unreadable"``, so a tool that armed its envelope
    (``_lib_exitcodes.arm_crash_envelope``) gets one on stdout here too —
    a tool no longer has to catch the error itself to keep that promise.

    Lives here rather than in ``_lib_exitcodes`` because that module is
    kept stdlib-only for the tools that import nothing else; this one
    already owns ``yaml`` and the error class.
    """
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except YamlFileError as exc:
            # ``safe_label``: the path is an untrusted filename (#1538) —
            # measured, a ``\x1b[31m`` in the name reached the terminal raw.
            die_caller_error(f"ERROR: cannot read {safe_label(exc)}",
                             reason="yaml_file_unreadable")
    return _wrapped  # type: ignore[return-value]


def find_metric_dictionary(tool_dir: str) -> "str | None":
    """Locate ``metric-dictionary.yaml`` for a tool living in *tool_dir*.

    The image flattens every tool next to the dictionary (build.sh); the
    repository keeps the tools under ``scripts/tools/ops/`` and the dictionary
    one level up. Looking only beside the tool — what analyze-gaps and migrate
    did — silently yielded an empty dictionary for every reader who ran
    ``python3 scripts/tools/ops/<tool>.py`` as the docs teach (issue 1513).
    Returns the first file that exists, else ``None`` so the caller can say so.
    """
    for candidate in (os.path.join(tool_dir, "metric-dictionary.yaml"),
                      os.path.join(os.path.dirname(os.path.abspath(tool_dir)),
                                   "metric-dictionary.yaml")):
        if os.path.isfile(candidate):
            return candidate
    return None


class OutputWriteError(OSError):
    """A secure writer could not write (or create the directory for) *path*.

    #1641. Raised by :func:`write_text_secure` / :func:`write_json_secure` /
    :func:`ensure_dir` in place of the bare ``OSError`` family, so that the
    ONE class of failure — "the output path is unusable" — has one name and
    one message shape across every tool, instead of a traceback at rc=1
    (which in this repo reads as EXIT_VIOLATION, "your config has a
    violation", for what is a mistyped ``-o``).

    It subclasses :class:`OSError` on purpose: every call site that already
    guarded with ``except OSError`` keeps working unchanged, and ``errno`` /
    ``strerror`` / ``filename`` are populated from the original exception.

    Attributes:
        path:   The path that could not be written.
        flag:   The CLI flag the path came from (``"-o/--output"``), or
                ``None`` when the path is derived internally.
        cause:  The original :class:`OSError`.
        action: ``"write"`` (default) or ``"create directory"``.

    ``str(exc)`` is the operator-facing line::

        cannot write <path>: <strerror> (errno <n>) — check the value given to <flag>
        cannot write <path>: <strerror> (errno <n>) — internal output path, this is a bug or an unwritable workspace
    """

    def __init__(
        self,
        path: Any,
        cause: OSError,
        *,
        flag: Optional[str] = None,
        action: str = "write",
    ) -> None:
        self.path = str(path)
        self.flag = flag
        self.cause = cause
        self.action = action
        strerror = getattr(cause, "strerror", None) or str(cause) or type(cause).__name__
        super().__init__(getattr(cause, "errno", None), strerror, self.path)

    def __str__(self) -> str:
        detail = self.strerror or str(self.cause)
        if self.errno is not None:
            detail = f"{detail} (errno {self.errno})"
        if self.flag:
            hint = f"check the value given to {self.flag}"
        else:
            hint = "internal output path, this is a bug or an unwritable workspace"
        return f"cannot {self.action} {self.path}: {detail} — {hint}"


def _die_on_write_error(exc: OutputWriteError, exit_code: Optional[int]) -> None:
    """Print the one-line message to stderr and exit — no traceback; an armed
    ``--json`` envelope says ``reason: output_unwritable`` (#3115)."""
    # #1538: the path is operator argv — still escape control characters
    # before it reaches a terminal (blind review).
    die_caller_error(f"ERROR: {safe_label(str(exc))}", reason="output_unwritable",
                     code=exit_code)


# #3105: ``0o600`` on a regular file only (both writers below). A device or
# FIFO (``-o /dev/null``) is left alone — a socket already fails the open: as root the chmod would change the
# device for every process on the host (a ``crw-------`` /dev/null), and as
# anyone else it fails (EPERM) after the content was already written. A
# regular file owned by someone else still fails the chmod and the write is
# reported as failed — the ``0o600`` the caller asked for is not dropped.


def write_text_secure(path: str, content: str, *, flag: Optional[str] = None) -> None:
    """Write text to *path* with UTF-8 encoding, LF endings, and ``0o600``
    (a regular file only; a device or FIFO keeps its mode, #3105).

    Centralises the SAST-mandated pattern::

        with open(path, "w", encoding="utf-8", newline="\\n") as f:
            f.write(content)
        Path(path).chmod(0o600)   # regular files only (#3105)

    ⛔ ``newline="\\n"`` is load-bearing, not cosmetic. Without it Python's
    text layer translates every ``\\n`` to ``os.linesep``, so the SAME
    generator emits LF on Linux/CI and CRLF on a Windows host. Since reads go
    through universal-newline translation, the CRLF is invisible to the tool's
    own ``--check`` and ``.gitattributes`` (``* text=auto eol=lf``) normalises
    it away at commit — so the staged diff stays correct and nothing fails.
    What breaks is the *working copy*: it diverges byte-wise from every other
    file in the tree, which defeats byte-level comparisons (mutation harnesses
    asserting a file was restored byte-identical) and emits a confusing
    "CRLF will be replaced by LF" warning on every subsequent ``git diff``.

    Note this pins LF unconditionally, which is correct for every current
    caller. If a caller ever needs to emit a Windows-shell script
    (``.bat`` / ``.cmd`` / ``.ps1`` — the only paths ``.gitattributes`` marks
    ``eol=crlf``), it must NOT use this helper.

    Args:
        path: Filesystem path to write.
        content: Text content.
        flag: The CLI flag *path* came from (e.g. ``"-o/--output"``), named
              in the error message; ``None`` for an internally derived path.

    Raises:
        OutputWriteError: on any ``OSError`` from the write or the chmod
            (#1641). It IS an ``OSError``, so an existing ``except OSError``
            still catches it. Tool code on a CLI path should prefer
            :func:`write_text_or_die`, which turns it into rc=2 + one line.
    """
    target = Path(path)
    try:
        target.write_text(content, encoding="utf-8", newline="\n")
        if stat.S_ISREG(os.stat(path).st_mode):  # #3105: not a device
            os.chmod(path, 0o600)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_json_secure(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    flag: Optional[str] = None,
) -> None:
    """Write *data* as JSON to *path* with ``0o600`` permissions (a regular
    file only; a device or FIFO keeps its mode, #3105).

    Args:
        path: Filesystem path to write.
        data: JSON-serializable object.
        indent: JSON indentation (default 2).
        ensure_ascii: If ``False`` (default), allow non-ASCII characters.
        flag: The CLI flag *path* came from; see :func:`write_text_secure`.

    ``newline="\\n"`` is load-bearing for the same reason as
    :func:`write_text_secure` — ``json.dump`` emits ``\\n`` between lines and
    the text layer would translate every one of them to CRLF on a Windows
    host. ``.gitattributes`` pins ``*.json`` to ``eol=lf``.

    Raises:
        OutputWriteError: on any ``OSError`` from the open / write / chmod
            (#1641), same contract as :func:`write_text_secure`. A
            non-serialisable *data* still raises ``TypeError`` — that is a
            programming error, not an output-path problem.
    """
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=indent, ensure_ascii=ensure_ascii)
        if stat.S_ISREG(os.stat(path).st_mode):  # #3105: not a device
            os.chmod(path, 0o600)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_text_or_die(
    path: str,
    content: str,
    *,
    flag: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> None:
    """:func:`write_text_secure`, but an unusable path ends the process.

    #1641. On :class:`OutputWriteError` prints ``ERROR: <message>`` to stderr
    and exits with *exit_code* (default: the process caller-error code,
    ``EXIT_CALLER_ERROR`` = 2 unless the tool set another — the exit-code
    SSOT's "IO failure" cell) — no traceback, and NOT rc=1, which
    would read as "your config has a violation".

    Args:
        path: Filesystem path to write.
        content: Text content.
        flag: The CLI flag *path* came from (e.g. ``"-o/--output"``). Pass it
              whenever the path is operator-supplied so the message can say
              which flag to check; leave ``None`` for internal paths.
        exit_code: Process exit code on failure.
    """
    try:
        write_text_secure(path, content, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def write_json_or_die(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    flag: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> None:
    """:func:`write_json_secure`, but an unusable path ends the process.

    Same contract as :func:`write_text_or_die` (#1641).
    """
    try:
        write_json_secure(path, data, indent=indent, ensure_ascii=ensure_ascii, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


# ---------------------------------------------------------------------------
# #3107 v2: the two output entry points.
#
# OPERATOR OUTPUT — a path the operator named on a flag (``--findings-json``,
# ``--jwks-out``, ``-o``…). The kernel opens it, the way ``>`` and GNU tools
# do: no userspace path walk, no temp file, no rename. ``-`` is stdout.
# REPO ARTIFACT — a committed file a regen tool rewrites (doc maps, index
# tables, state manifests). Atomic replace, the ``_atomic_write`` contract.
# ---------------------------------------------------------------------------

STDOUT_PATH = "-"


def claim_stdout(owner: str) -> None:
    """Record that *owner* (a flag, or ``"report"`` for the tool's own stdout
    report) writes to stdout this run. A second claimant is a caller error:
    two documents on one stream cannot be told apart by a consumer. The
    record is the run's exit state (``_lib_exitcodes.claim_stdout_owner``:
    ``run()`` starts each run without one)."""
    other = _lib_exitcodes.claim_stdout_owner(owner)
    if other is not None:
        raise OutputWriteError(
            STDOUT_PATH, OSError(None, f"standard output is already used by {other}"), flag=owner)


def _is_our_stdout(pre: Optional[os.stat_result]) -> bool:
    """Is *pre* (the kernel's stat of the target) the file, pipe or socket
    our fd 1 already writes to?  Reopening a regular file with O_TRUNC wipes
    what the shell redirect already holds; reopening a pipe interleaves two
    documents. Either way it is treated as ``-``. A character device (tty,
    /dev/null) is not: reopening one is harmless."""
    if pre is None or not (stat.S_ISREG(pre.st_mode) or stat.S_ISFIFO(pre.st_mode)
                           or stat.S_ISSOCK(pre.st_mode)):
        return False
    try:
        out = os.fstat(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return (out.st_dev, out.st_ino) == (pre.st_dev, pre.st_ino)


def targets_stdout(path: str) -> bool:
    """Will an operator output at *path* be written through stdout (``-``,
    or the file / pipe / socket fd 1 already is)?  A tool that also prints a
    receipt to stdout moves it to stderr when this is true."""
    if path == STDOUT_PATH:
        return True
    try:
        return _is_our_stdout(os.stat(path))
    except OSError:
        return False


class _StdoutSink(io.TextIOBase):
    """Text sink over ``sys.stdout`` that never closes it."""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:  # type: ignore[override]
        return sys.stdout.write(s)

    def flush(self) -> None:
        sys.stdout.flush()


_O_OPERATOR = (os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_BINARY", 0))


def _kernel_open_operator_output(path: str, mode: int, append: bool) -> tuple[int, bool]:
    """``open(2)`` *path* for writing; return ``(fd, created)``.

    O_NONBLOCK only for the open itself: a FIFO with no reader fails with
    ENXIO instead of blocking forever; it is cleared before any write. A
    socket fails the open (ENXIO) on its own.
    """
    nonblock = getattr(os, "O_NONBLOCK", 0)
    flags = _O_OPERATOR | (os.O_APPEND if append else os.O_TRUNC) | nonblock
    fd = os.open(path, flags, mode)
    try:
        # Only where O_NONBLOCK was set: on Windows (3.12+) set_blocking
        # works on pipes only and fails on a regular file's fd.
        if nonblock:
            os.set_blocking(fd, True)
        st = os.fstat(fd)
        try:
            # The name as it is NOW (after the open): a symlink swapped in
            # before the open, or after, is not this euid's file to chmod.
            now = os.lstat(path)
            via_link = (not stat.S_ISREG(now.st_mode)
                        or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino))
        except OSError:
            via_link = True
        # R2 + V15 (#3107 risk 2, owner 2026-10-24): a regular file this euid
        # owns gets *mode* before any byte is written — created by this call
        # or already there (an operator-made 0644 file would leave a findings
        # file world-readable). Not someone else's file (V2: no chmod, the
        # write succeeds), not a device, and not a file reached through a
        # symlink (its target is the link owner's business). A failed fchmod
        # is the write's failure (rc 2).
        own = (stat.S_ISREG(st.st_mode) and not via_link
               and st.st_uid == getattr(os, "geteuid", lambda: st.st_uid)())
        if own and stat.S_IMODE(st.st_mode) != mode:
            os.fchmod(fd, mode) if hasattr(os, "fchmod") else os.chmod(path, mode)
        return fd, stat.S_ISREG(st.st_mode)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def open_operator_output(path: str, *, flag: Optional[str], mode: int = 0o600,
                         append: bool = False) -> Iterator[io.TextIOBase]:
    """Open an operator-named output path for UTF-8/LF text writing.

    * ``-`` ⇒ stdout (claimed for *flag*; never closed).
    * the regular file stdout already writes to ⇒ same as ``-``.
    * anything else ⇒ ``open(2)`` by the kernel (``O_CREAT|O_TRUNC``, or
      ``O_APPEND``); *mode* applies to a regular file this euid owns and did
      not reach through a symlink — created now or already there (V15).
    * any ``OSError`` ⇒ :class:`OutputWriteError` naming *path* and *flag*.
      A write into a regular file that raises leaves it empty, not a prefix
      (a process killed mid-write can still leave one).
    """
    try:
        try:
            pre = os.stat(path) if path != STDOUT_PATH else None
        except OSError:
            pre = None
        if path == STDOUT_PATH or _is_our_stdout(pre):
            claim_stdout(flag or path)
            sink = _StdoutSink()
            yield sink
            sink.flush()
            return
        fd, regular = _kernel_open_operator_output(path, mode, append)
        fh = io.open(fd, "w", encoding="utf-8", newline="\n", closefd=True)
        try:
            yield fh
            fh.flush()
        except BaseException:
            if regular and not append:
                with contextlib.suppress(OSError, ValueError):
                    fh.flush()
                with contextlib.suppress(OSError, ValueError):
                    os.ftruncate(fh.fileno(), 0)
            raise
        finally:
            with contextlib.suppress(OSError, ValueError):
                fh.close()
    except OutputWriteError:
        raise
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_operator_output(path: str, content: str, *, flag: Optional[str],
                          mode: int = 0o600, append: bool = False) -> None:
    """Write *content* to an operator-named path; see :func:`open_operator_output`."""
    with open_operator_output(path, flag=flag, mode=mode, append=append) as fh:
        fh.write(content)


def write_operator_output_or_die(path: str, content: str, *, flag: Optional[str],
                                 mode: int = 0o600, append: bool = False,
                                 exit_code: Optional[int] = None) -> None:
    """:func:`write_operator_output`; an unusable path exits (rc 2, one line)."""
    try:
        write_operator_output(path, content, flag=flag, mode=mode, append=append)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


def check_repo_artifact(path: Any, *, flag: Optional[str] = None) -> str:
    """The preconditions of :func:`write_repo_artifact`, callable before a
    tool READS the artifact it will rewrite (a FIFO or ``/dev/stdout`` would
    otherwise block the read). Returns ``os.fspath(path)``."""
    p = os.fspath(path)
    if p == STDOUT_PATH:
        raise OutputWriteError(p, OSError(None, "an artifact cannot be standard output"), flag=flag)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        # Absent — or a dangling link the kernel let us follow (it refuses
        # the sticky-directory case with EACCES above): the _atomic_write
        # contract decides, as before.
        st = None
    except OSError as exc:
        raise OutputWriteError(p, exc, flag=flag) from exc
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise OutputWriteError(p, OSError(None, "not a regular file"), flag=flag)
    if st is not None:
        # /dev/stdout, /proc/self/fd/N … resolve (in userspace) to the file
        # the shell redirected; replacing it would discard that stream.
        for fd in (0, 1, 2):
            try:
                o = os.fstat(fd)
            except OSError:
                continue
            if (o.st_dev, o.st_ino) == (st.st_dev, st.st_ino):
                raise OutputWriteError(p, OSError(None, "is this process's standard stream"), flag=flag)
    return p


def write_repo_artifact(path: Any, content: str, *, flag: Optional[str] = None,
                        mode: int = 0o644, atomic_only: bool = False) -> None:
    """Atomically rewrite a committed artifact a regen tool owns.

    The ``_atomic_write`` contract, behind two kernel-side preconditions:
    the target (or, when absent, its directory) must ``stat`` through the
    kernel — so ``fs.protected_symlinks`` and directory permissions answer
    before any userspace ``realpath`` runs — and an existing target must be
    a regular file. ``-``, devices, FIFOs and sockets are
    refused with :class:`OutputWriteError`.
    """
    p = check_repo_artifact(path, flag=flag)
    # Imported here: _lib_atomic_write imports this module (OutputWriteError).
    from _lib_atomic_write import atomic_write_text  # noqa: E402  (implementation)
    atomic_write_text(p, content, mode=mode, flag=flag, atomic_only=atomic_only)


def ensure_dir(path: Any, *, flag: Optional[str] = None) -> None:
    """``mkdir -p`` *path*, raising :class:`OutputWriteError` on failure.

    #1641. Output-directory tools create the directory themselves before the
    first secure write, and ``os.makedirs(..., exist_ok=True)`` raises the
    same ``OSError`` family (``NotADirectoryError`` when a path component is
    a file, ``PermissionError``, …) OUTSIDE the writer. Routing the mkdir
    through here keeps the failure in the one class, with the same message
    shape (``cannot create directory <path>: …``).
    """
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag, action="create directory") from exc


def ensure_dir_or_die(
    path: Any,
    *,
    flag: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> None:
    """:func:`ensure_dir`, but an unusable path ends the process (rc=2)."""
    try:
        ensure_dir(path, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


class PathProblem(NamedTuple):
    """Why a caller-given path cannot be used (`classify_path`)."""

    reason: str
    """`--json` reason: ``{dir,file,path}_not_{found,accessible}``."""
    message: str
    """The stderr line, without ``ERROR:``."""
    missing: bool
    """True only when nothing is at the path (ENOENT, ENOTDIR, ELOOP)."""


#: #3276: what a stat errno means for a caller-given path. Only "nothing is
#: there" is not-found; every other errno (EACCES under a parent the caller
#: cannot enter, EIO, …) is not-accessible — never folded into "not found".
_STAT_MISSING = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


def classify_path(path: Any, kind: str, *, flag: Optional[str] = None) -> Optional[PathProblem]:
    """None when *path* (given by the caller, via *flag*) is usable as a
    *kind* — ``"dir"``, ``"file"`` or ``"any"`` — else the `PathProblem`.

    #3276. The one judgement of a caller-given path, replacing each tool's
    ``Path.is_dir()`` / ``exists()`` / ``is_file()`` and ``os.path.*``:
    which errnos those swallow differs across Python versions (3.13 raises
    EACCES, 3.14 answers False) and ``os.path.*`` already answers False, so
    a path under a parent the caller cannot enter was a traceback or a
    "not found". Here ``os.stat`` decides by errno.

    * ``"dir"``: a directory. Only stat: whether its content can be listed
      is the reader's verdict (for a conf.d, da-guard's ``walk_error``).
    * ``"file"``: anything but a directory, that opens for reading (a FIFO
      too: opened non-blocking, never read).
    * ``"any"``: anything that stats.
    """
    # '' is SUPPLIED (an unset shell variable) and must stay visible.
    shown = safe_label(str(path)) if str(path) else "''"
    label = f"{flag} {shown}" if flag else shown
    noun = {"dir": "dir", "file": "file", "any": "path"}[kind]
    try:
        st = os.stat(path)
    except OSError as exc:
        missing = exc.errno in _STAT_MISSING
        return PathProblem(f"{noun}_not_{'found' if missing else 'accessible'}",
                           f"{label}: {exc.strerror}", missing)
    if kind == "dir" and not stat.S_ISDIR(st.st_mode):
        return PathProblem("dir_not_found", f"{label}: not a directory", False)
    if kind == "file":
        if stat.S_ISDIR(st.st_mode):
            return PathProblem("file_not_found", f"{label}: is a directory, not a file", False)
        try:
            os.close(os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)))
        except OSError as exc:
            return PathProblem("file_not_accessible", f"{label}: {exc.strerror}", False)
    return None


def require_path(path: Any, kind: str, *, flag: Optional[str] = None) -> None:
    """`classify_path`, raising `CallerError` (rc 2 under ``run()``, the
    reason in the ``--json`` envelope) when *path* is not usable."""
    problem = classify_path(path, kind, flag=flag)
    if problem is not None:
        raise _lib_exitcodes.CallerError(problem.message, problem.reason)


def require_dir(path: Any, *, flag: Optional[str] = None) -> None:
    """`require_path` for a directory."""
    require_path(path, "dir", flag=flag)


def require_file(path: Any, *, flag: Optional[str] = None) -> None:
    """`require_path` for a file."""
    require_path(path, "file", flag=flag)


def raise_path_problems(problems: "list[Optional[PathProblem]]") -> None:
    """Raise one `CallerError` naming every problem in *problems* (one
    ``ERROR:`` line each, Nones skipped) with the first one's reason; return
    when there is none. For a tool given several paths at once."""
    found = [p for p in problems if p is not None]
    if found:
        raise _lib_exitcodes.CallerError("\nERROR: ".join(p.message for p in found),
                                         found[0].reason)


def _output_write_names_target(exc: OSError, path: Any) -> bool:
    """Does *exc* name *path* (or a parent of it) as the thing that failed?

    The decision procedure behind :func:`output_write`'s "convert or let it
    fly" split, kept as a named function so the tests exercise the SAME code
    the context manager runs (rulebook D-05d).

    * Neither ``filename`` nor ``filename2`` is set ⇒ TRUE. ``Path.mkdir``,
      ``Path.chmod`` and ``os.replace`` populate them, but plenty of failures
      arrive bare (an ``OSError`` re-raised by a helper, ``shutil`` wrapping
      an ``errno`` it built itself), and inside a block that exists only to
      produce the output file, bare is the output.
    * Either one resolves to *path* itself, or to an ANCESTOR of it ⇒ TRUE.
      The ancestor arm is what makes ``os.makedirs("a/b/c")`` convertible:
      the component that could not be created is ``a/b``, never the path the
      caller named.
    * Anything else ⇒ FALSE: the failure is about some OTHER file, and
      calling it "cannot write <output> — check the value given to -o" would
      be a lie that sends the operator to the wrong flag.

    Comparison goes through ``os.fspath`` + ``Path(...).resolve(strict=False)``
    so ``out`` and ``/abs/cwd/out`` are the same path; ``strict=False``
    because the whole point is that the path does not exist yet. A filename
    that is not path-like (a raw file descriptor ``int``, ``bytes``) cannot be
    compared and counts as "names something else" — fail towards letting the
    original exception through with its own type and message intact.

    An EMPTY filename is treated as absent rather than as ``""`` (which would
    resolve to the current directory and match anything under it).

    ⛔ ``resolve`` can raise ``OSError`` too, not only ``TypeError`` /
    ``ValueError``: a RELATIVE path needs the cwd, and a cwd that was deleted
    out from under the process makes ``os.getcwd()`` raise
    ``FileNotFoundError``. Letting that escape would replace the write error
    the operator caused with a chained traceback at rc=1 from the predicate
    that was supposed to classify it — the exact failure this whole ticket is
    about, produced by its own machinery. Both arms therefore catch it and
    fall back to "cannot compare", which lets the ORIGINAL exception through
    untouched.
    """
    named = [n for n in (getattr(exc, "filename", None), getattr(exc, "filename2", None))
             if n is not None and n != ""]
    if not named:
        return True
    try:
        target = Path(os.fspath(path)).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    for name in named:
        try:
            candidate = Path(os.fspath(name)).resolve(strict=False)
        except (TypeError, ValueError, OSError):
            continue  # fd number / bytes / no cwd: not comparable
        if candidate == target or candidate in target.parents:
            return True
    return False


@contextlib.contextmanager
def output_write(
    path: Any,
    *,
    flag: Optional[str],
    action: str = "write",
) -> Iterator[None]:
    """Make a RAW write to *path* fail the way the secure writers do (#1789).

    :func:`write_text_secure` and friends already turn "the output path is
    unusable" into one :class:`OutputWriteError`, but a tool that writes with
    ``open()``, ``Path.mkdir()``, ``shutil.copy2()`` or ``csv.writer`` cannot
    use them without changing the bytes it emits. This wraps such a site
    instead of converting it::

        with output_write(out_dir, flag="-o/--output-dir", action="create directory"):
            out_dir.mkdir(parents=True, exist_ok=True)

    so that a mistyped ``-o`` ends in the same one-line ``ERROR: cannot …``
    at rc=2 as every ``_or_die`` call site, instead of a traceback at rc=1
    (which a CI consumer reads as ``EXIT_VIOLATION``, "your config has a
    finding"). The bytes and the permissions of the SUCCESS path are
    untouched: on success this context manager does nothing at all.

    Args:
        path: The output path the block is trying to produce.
        flag: The CLI flag *path* came from (``"-o/--output"``), named in the
              message. Keyword-only and REQUIRED — pass ``None`` explicitly
              for an internally derived path, so that every wrapped site has
              answered the question rather than inherited a default.
        action: The verb in the message: ``"write"`` (default),
                ``"create directory"``, ``"copy into"``, …

    ⚠️ **Pass the path the block acts ON, not the path the tool is ultimately
    producing.** For a ``mkdir`` in front of a file write that means the
    PARENT: wrapping ``out.parent.mkdir(...)`` with ``path=out`` prints
    "cannot create directory /srv/reports/health.json" — a sentence about a
    path nobody was creating, pointing at a file that is not the problem.
    Measured on six sites in this batch before they were split (#1789); the
    behavioural gate now parses the verb out of the line and checks the path
    against it, so the two cannot drift apart again.

    Raises:
        OutputWriteError: for an ``OSError`` raised inside the block that
            :func:`_output_write_names_target` attributes to *path*.
        Anything else: unchanged, including an ``OSError`` about a DIFFERENT
            file and an :class:`OutputWriteError` that a nested secure writer
            already raised (never double-wrapped — the inner one already
            carries the right path, flag and action).

    ⛔ **Why "an OSError about a different file" flies through, and why that
    is not over-caution.** ``ops/assemble_config_dir`` copies each source
    file into the output directory with ``shutil.copy2(src, dst)``; ``src``
    comes from ``--sources``. A source ENTRY that is itself a directory (a
    ``--sources`` tree holding a directory named ``x.yaml`` — measured, not
    hypothetical) raises ``IsADirectoryError`` with ``filename`` pointing at
    the SOURCE. Converting that would print "cannot copy into
    <output>/x.yaml … check the value given to --output" and send the
    operator to edit the one flag that is correct. Same for any input file
    read inside the wrapped block.

    ⚠️ **The design rule for where to put the ``with``.** This converts an
    exception on its way OUT of the block, so a NARROW handler between the
    raise and the ``with`` swallows it first and this never sees it — an
    ``except FileNotFoundError`` inside the block, or a ``try/except OSError:
    continue`` loop around the write, keeps its old behaviour and no rc
    changes. That is a silent miss, not a loud one: wrap the site, then read
    outwards for handlers that already intercept the write. Measured over the
    sites this ticket wraps: the only handlers INSIDE a wrapped block are the
    three in ``ops/state_reconcile.write_json`` — ``mkstemp``'s converts to
    ``OutputWriteError`` by hand, the cleanup ``except BaseException``
    re-raises, and the ``except OSError: pass`` swallows the temp-file
    ``unlink`` only, never the write. And reading out to the enclosing ``def``
    is NOT far enough: ``ops/da_assembler``'s swallowing ``except Exception``
    sits two levels out from the ``with`` — past ``write_rendered``, in its
    caller ``reconcile_one`` — and only an explicit re-raise added there lets
    the conversion reach the decorator.
    """
    try:
        yield
    except OutputWriteError:
        raise
    except OSError as exc:
        if not _output_write_names_target(exc, path):
            raise
        raise OutputWriteError(path, exc, flag=flag, action=action) from exc


def exit_on_output_write_error(fn: _F) -> _F:
    """Decorate a CLI ``main`` so an unusable OUTPUT path exits 2, named.

    The sister of :func:`exit_on_yaml_file_error` for the write direction
    (#1789): :class:`OutputWriteError` — raised by the secure writers, by
    :func:`ensure_dir`, or by a raw site wrapped in :func:`output_write` —
    becomes ``ERROR: cannot <action> <path>: … — check the value given to
    <flag>`` on stderr plus ``sys.exit(EXIT_CALLER_ERROR)``, with no
    traceback.

    Use it on tools whose write sites are spread across several helpers, so
    the class is closed once at the entry point instead of at each site.

    ⚠️ Only :class:`OutputWriteError`, never a bare ``OSError``: an
    ``OSError`` that reached ``main`` from somewhere else (an unreadable
    INPUT, a socket) has no output path to name and no flag to point at, and
    turning it into this message would misattribute it. A tool that armed
    its ``--json`` envelope gets one here, ``reason: output_unwritable``
    (#3115) — no need to catch the error itself.
    """
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except OutputWriteError as exc:
            _die_on_write_error(exc, None)
    return _wrapped  # type: ignore[return-value]


def write_onboard_hints(
    output_dir: str,
    hints: dict[str, Any],
    *,
    flag: Optional[str] = None,
) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).
        flag: The CLI flag *output_dir* came from; see :func:`write_text_secure`.

    Returns:
        Absolute path to the written file.

    Raises:
        OutputWriteError: see :func:`write_json_secure` (#1641).
    """
    path = str(Path(output_dir) / ONBOARD_HINTS_FILENAME)
    write_json_secure(path, hints, flag=flag)
    return path


def read_onboard_hints(path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read onboard hints JSON.

    Returns:
        Parsed dict, or ``None`` if file is missing / unreadable.
    """
    if not path or not Path(path).is_file():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_label(value: Any) -> str:
    """Neutralise control characters in an untrusted value before it is PRINTED.

    #1538. A tenant config *filename* is untrusted input — ``tenants/onboarding``
    and the tenant self-service flow both create files whose names the platform
    never chose. Every plain-text report in ``scripts/tools/`` interpolates those
    names (and the exception text derived from them) straight into ``print()``.
    Two things ride in on that:

    * **a newline** ends the current report line and starts a new one, so a file
      named ``evil\\n[PASS] all good\\nx.yaml`` prints ``[PASS] all good`` at
      column 0 — indistinguishable, to a human or to a ``grep '^\\[PASS\\]'``, from
      a verdict the tool actually emitted;
    * **an ESC (``\\x1b``)** starts an ANSI sequence, so the same channel can
      recolour the terminal, move the cursor, or clear the screen.

    Replacing each control char with ``?`` defuses both while keeping the payload
    visible (``\\x1b[2J`` prints as ``?[2J``) — the operator still sees that
    something odd is in the name, which deleting the characters would hide.

    ⛔ **Scope, stated so nobody over-reads it.** This covers C0
    ``\x00-\x1f``, DEL ``\x7f``, and the C1 range ``\x80-\x9f``.

    C1 was added in the #1538 review round, and not as widening for its own
    sake: ``U+0085`` (NEL) is *the same attack as the newline* — terminals that
    decode C1 treat it as a line break, so an unescaped NEL forges a report line
    exactly as ``\n`` does, which is the one thing this function exists to stop.
    An adversarial review measured it passing through ten already-fixed tools.

    ⚠️ Honest boundary on that evidence: what was measured is **byte
    passthrough**, not rendering. Nobody drove a real terminal to confirm NEL
    breaks the line there. C1 is covered because it belongs to the same attack
    class, not because the terminal behaviour was demonstrated.

    ⛔ This also means the class is NO LONGER byte-identical to the one
    ``compile_custom_alerts._safe_log`` carried from #1008 until this change: a
    C1 character that used to survive that tool's quarantine line now renders as
    ``?``. A deliberate behaviour change, not a refactoring accident.

    Still NOT covered, deliberately:

    * bidi overrides ``U+202A-U+202E`` / ``U+2066-U+2069``. They reorder a line
      **visually** without breaking it — a different attack class from forging a
      line, and folding it in here would blur what this function promises.
    * homoglyph or zero-width confusables.

    Those remain open. Do not read "went through ``safe_label``" as "is safe to
    render in an arbitrary terminal."

    This is an OUTPUT-layer helper, not a validator: it must not be used to
    sanitise a value on its way *into* a config, a filename, or a subprocess
    argument. It lives beside :func:`format_json_report` because they are the two
    halves of the same decision — ``--json`` carries its own escaping and this is
    the plain-text branch's equivalent. ⛔ Never apply it to data destined for the
    ``--json`` branch: that would corrupt machine-readable output.

    ⚠️ **``--json`` is NOT unconditionally safe, and an earlier wording here said
    it was.** ``json.dumps`` is only required to escape ``"``, ``\\`` and
    ``U+0000``–``U+001F``; under ``ensure_ascii=False`` (this repo's default, see
    :func:`format_json_report`) the **C1** range passes through verbatim —
    measured, ``json.dumps("a\\x85b", ensure_ascii=False)`` keeps the raw byte
    while ``"a\\x0ab"`` becomes ``\\n``. So a ``--json`` payload piped straight to
    a terminal that interprets C1 is still forgeable. That is EXISTING behaviour,
    deliberately not changed here: the byte-for-byte stability of ``--json`` is
    the mechanism guarantee this escaping rests on, and rewriting the serialized
    output would trade a measured guarantee for an unmeasured one. Tracked
    separately; do not read this paragraph as "handled".

    ⛔ Apply it to the FIELD, never to a whole rendered multi-line report — the
    report's own ``\\n`` separators are control characters too and would become
    ``?``, collapsing the layout.

    Args:
        value: Any value; coerced with ``str()``.

    Returns:
        *value* as text with every C0, DEL **and C1** character replaced by
        ``?`` — the class is ``[\\x00-\\x1f\\x7f-\\x9f]``. C1 is in because
        ``\\x85`` (NEL) forges a line exactly like ``\\n`` does.
    """
    return _CONTROL_CHARS_RE.sub("?", str(value))


def format_json_report(data: Any, **kwargs: Any) -> str:
    """Serialize data as pretty-printed JSON (ensure_ascii=False).

    Thin wrapper to eliminate ``json.dumps(data, indent=2, ensure_ascii=False)``
    duplication across 20+ tools.  Extra kwargs are forwarded to ``json.dumps``.
    """
    kwargs.setdefault("indent", 2)
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(data, **kwargs)


# ── Common argparse helpers ─────────────────────────────────────────
# Extracted in v2.4.0 Phase B to eliminate argparse boilerplate across 20+ tools.


def add_config_dir_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
    default: str | None = None,
    help_text: str = "Path to tenant config directory (conf.d/)",
) -> None:
    """Add ``--config-dir`` argument with standard defaults."""
    parser.add_argument(
        "--config-dir",
        required=required and default is None,
        default=default,
        help=help_text,
    )


def add_json_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "Output as JSON (for CI integration)",
) -> None:
    """Add ``--json`` boolean flag for machine-readable output."""
    parser.add_argument("--json", action="store_true", dest="json_output", help=help_text)


def add_ci_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "CI mode: exit 1 on any issue",
) -> None:
    """Add ``--ci`` boolean flag for CI exit-code behaviour."""
    parser.add_argument("--ci", action="store_true", help=help_text)


def add_prometheus_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
    help_text: str = "Prometheus URL (default: $PROMETHEUS_URL or http://localhost:9090)",
) -> None:
    """Add ``--prometheus`` argument with env-var fallback."""
    parser.add_argument(
        "--prometheus",
        # `... or "http://localhost:9090"` (not the get() default) so an
        # empty $PROMETHEUS_URL falls back to localhost too. This aligns the
        # empty-string semantics with entrypoint.py's inject_prometheus_env
        # (`if prom_url:`), which also treats "" as unset — otherwise a
        # deployment that sets PROMETHEUS_URL="" (e.g. a ConfigMap key that
        # resolves empty) would get an empty URL here but localhost via the
        # dispatcher, an inconsistency between the two fallback mechanisms.
        default=default or os.environ.get("PROMETHEUS_URL") or "http://localhost:9090",
        help=help_text,
    )
