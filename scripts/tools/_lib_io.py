"""File I/O and YAML helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

import yaml

from _lib_confd import warn_nested
from _lib_constants import ONBOARD_HINTS_FILENAME
# Leaf module (yaml + stdlib only), so no import cycle (#2114).
from _lib_yaml_keys import ExporterKeyLoader, load_exporter_keys
# No import cycle: _lib_exitcodes imports only sys + _lib_compat (#1641).
from _lib_exitcodes import EXIT_CALLER_ERROR

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
    :func:`load_yaml_file`. The strict sibling is
    :func:`load_yaml_file_strict_exporter_keys`; pick by what the caller
    read with before (#2123 decided which readers are strict).

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

_MERGE_TAG = "tag:yaml.org,2002:merge"


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
def _strict_exporter_key_class(raw_text_sequences: "frozenset[str]") -> type:
    """``StrictExporterKeyLoader``, or a subclass that also reads the lists
    under *raw_text_sequences* as source text — a class, because the strict
    entry points below build the loader themselves."""
    if not raw_text_sequences:
        return StrictExporterKeyLoader
    return type("StrictExporterKeyLoader_raw", (StrictExporterKeyLoader,),
                {"raw_text_sequences": raw_text_sequences})


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


def strict_load_exporter_keys(stream: Any, *,
                              raw_text_sequences: "frozenset[str] | tuple[str, ...]" = ()
                              ) -> Any:
    """:func:`strict_safe_load` whose mapping keys are the exporter's tenant
    ids (raw text, #2114). Same driver, same errors — only the loader class
    differs (:class:`StrictExporterKeyLoader`)."""
    return strict_safe_load(stream, loader=_strict_exporter_key_class(
        frozenset(raw_text_sequences)))


def strict_load_all_exporter_keys(stream: Any, *,
                                  raw_text_sequences: "frozenset[str] | tuple[str, ...]" = ()
                                  ) -> Iterator[Any]:
    """:func:`strict_safe_load_all` with exporter keys; lazy, so
    ``next(...)`` reads — and checks — the first document only."""
    return strict_safe_load_all(stream, loader=_strict_exporter_key_class(
        frozenset(raw_text_sequences)))


def load_yaml_file_strict_exporter_keys(
    path: Optional[str],
    default: Any = None,
    *,
    raw_text_sequences: "tuple[str, ...] | frozenset[str]" = (),
) -> Any:
    """:func:`load_yaml_file_strict` whose mapping keys are raw text (#2114).

    Same contract as :func:`load_yaml_file_strict` — same pure parser, same
    UTF-8 decode, *default* for a missing or empty file, and a duplicate
    key (by the exporter's identity: ``123`` and ``"123"`` are one key)
    raises the same :class:`YamlFileError` a syntax error does.
    """
    # Spelled apart from its siblings: the mutation catalogue anchors on
    # their lines being unique in the file.
    if not path or not os.path.isfile(path):
        return default
    try:
        text = Path(path).read_bytes().decode("utf-8")
        named = io.StringIO(text)
        named.name = str(path)
        data = strict_load_exporter_keys(
            named, raw_text_sequences=raw_text_sequences)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise YamlFileError(str(path), exc) from exc
    return default if data is None else data


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
    layer that raised it stays visible. Tools that owe stdout a ``--json``
    envelope on this path (``backtest_threshold``, ``policy_engine``)
    catch the error themselves at the load site instead of using this.

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
            print(f"ERROR: cannot read {safe_label(exc)}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    return _wrapped  # type: ignore[return-value]


def iter_yaml_files(
    config_dir: str,
    *,
    skip_reserved: bool = True,
) -> list[tuple[str, str]]:
    """List YAML files in *config_dir*, sorted deterministically.

    The extension test is CASE-INSENSITIVE (#1537): the exporter lowercases
    the entry name before testing ``.yaml`` / ``.yml``, so it reads
    ``upper.YAML`` and merges it into the config it serves. An exact-suffix
    test here made that file invisible — not reported, simply absent — which
    is #1911's divergence with the extension as the axis rather than
    directory depth. Nothing here greps the Go source (#1448 measured that a
    Python guard asserting things about Go source text goes red on
    legitimate Go refactors while stating the opposite of the truth); the
    shared conf.d name classification matrix under ``tests/shared/`` is what
    keeps this side and the exporter's side pinned to one rule.

    Args:
        config_dir: Path to the configuration directory.
        skip_reserved: If ``True`` (default), skip files whose names
                       start with ``_`` or ``.`` (reserved / dotfiles).
                       ⚠️ It gates BOTH prefixes, so ``skip_reserved=False``
                       also stops hiding dotfiles — measured, ``.hidden.yaml``
                       comes back. That differs from ``_lib_confd``, whose
                       hidden filter is unconditional; a caller that wants
                       "reserved control files too, but still no dotfiles"
                       has to filter for itself.

    Returns:
        List of ``(filename, full_path)`` tuples, sorted by filename.
    """
    if not config_dir:
        return []
    base = Path(config_dir)
    if not base.is_dir():
        return []
    # #1911: flat read — a hierarchical conf.d must not look empty.
    # tool= omitted on purpose: this helper is reached from several
    # entry points, so the message should name the command the
    # operator ran, not this function.
    warn_nested(base)
    result: list[tuple[str, str]] = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        fname = entry.name
        lower = fname.lower()
        if not (lower.endswith(".yaml") or lower.endswith(".yml")):
            continue
        if skip_reserved and (fname.startswith("_") or fname.startswith(".")):
            continue
        if entry.is_file():
            result.append((fname, str(entry)))
    return result


def load_tenant_configs(config_dir: str) -> dict[str, dict[str, Any]]:
    """Load all tenant configurations from a config directory.

    Handles both the ``{tenants: {name: {...}}}`` wrapper format
    (used in ``conf.d/``) and the flat single-tenant format.
    Files starting with ``_`` or ``.`` are skipped.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        Dict mapping ``tenant_name`` → ``config_dict``.  Empty dict when
        *config_dir* is missing or holds no eligible files.

        Tenant names are always ``str``: the key's source text, which is
        what the exporter serves (#2114) — an unquoted ``010:`` is ``"010"``
        and ``yes:`` is ``"yes"`` (PyYAML's own typing made them ``8`` and
        ``True``). Mapping keys inside a config are text too; values keep
        PyYAML's types. Same pure-Python parser as :func:`load_yaml_file`.
        ⚠️ This is true of THIS helper's callers; tools that read tenant ids
        some other way may still use PyYAML's typing (tracked in #2115).

        ⚠️ A document that parses to a non-mapping is skipped, but an EMPTY
        file is not: ``load_yaml_file`` turns it into the ``{}`` default, so
        the file registers a tenant named after it with no thresholds. Same
        for a comments-only file. Measured, and load-bearing for every caller
        that counts tenants.

    Raises:
        Anything raised while listing the directory or reading a file
        propagates. :class:`YamlFileError` (bad syntax, a duplicate mapping
        key — #2123 — OR non-UTF-8 content, naming the file — #1654; it is a
        ``yaml.YAMLError``) and ``OSError``
        are the common ones, but this is deliberately NOT a closed list — a
        deeply nested document raises ``RecursionError``, which is a sibling
        of none of them, and an unreadable *directory* raises from
        :func:`iter_yaml_files` rather than from :func:`load_yaml_file`.
        Callers that need a CLI exit code should catch broadly at their entry
        point rather than name types here.

    ⛔ This previously documented itself as returning an "empty dict on any
    error", which was never true — the exceptions above have always
    propagated. The wording mattered because it invites callers to treat a
    malformed config directory as an empty one, i.e. to fail OPEN on exactly
    the input that should be loudest. Callers that need a CLI exit code
    should map these to ``EXIT_CALLER_ERROR`` at their own entry point (see
    ``scripts/tools/ops/config_diff.py:main``); swallowing them *here* would
    silently turn "your config is broken" into "you have no tenants" for
    every one of this helper's callers at once.
    """
    configs: dict[str, dict[str, Any]] = {}
    for fname, fpath in iter_yaml_files(config_dir):
        # Strict (#2123): a file holding a key twice is one the exporter
        # rejects, so it raises YamlFileError here like any unreadable file
        # instead of registering whichever value PyYAML kept last.
        # #2114: tenant ids are the keys' source TEXT, as the exporter keys
        # them (`010:` is "010", not 8) — and `123:` / `"123":` in one
        # mapping are that duplicate.
        raw = load_yaml_file_strict_exporter_keys(fpath, default={})
        if not isinstance(raw, dict):
            continue
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            for t_name, t_data in raw["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = t_data
        else:
            tenant = fname.rsplit(".", 1)[0]
            configs[tenant] = raw
    return configs


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


def _die_on_write_error(exc: OutputWriteError, exit_code: int) -> None:
    """Print the one-line message to stderr and exit — no traceback."""
    # #1538: the path is operator argv — still escape control characters
    # before it reaches a terminal (blind review).
    print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)
    sys.exit(exit_code)


def write_text_secure(path: str, content: str, *, flag: Optional[str] = None) -> None:
    """Write text to *path* with UTF-8 encoding, LF endings, and ``0o600``.

    Centralises the SAST-mandated pattern::

        with open(path, "w", encoding="utf-8", newline="\\n") as f:
            f.write(content)
        Path(path).chmod(0o600)

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
        target.chmod(0o600)
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
    """Write *data* as JSON to *path* with ``0o600`` permissions.

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
        Path(path).chmod(0o600)
    except OSError as exc:
        raise OutputWriteError(path, exc, flag=flag) from exc


def write_text_or_die(
    path: str,
    content: str,
    *,
    flag: Optional[str] = None,
    exit_code: int = EXIT_CALLER_ERROR,
) -> None:
    """:func:`write_text_secure`, but an unusable path ends the process.

    #1641. On :class:`OutputWriteError` prints ``ERROR: <message>`` to stderr
    and exits with *exit_code* (default ``EXIT_CALLER_ERROR`` = 2, the
    exit-code SSOT's "IO failure" cell) — no traceback, and NOT rc=1, which
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
    exit_code: int = EXIT_CALLER_ERROR,
) -> None:
    """:func:`write_json_secure`, but an unusable path ends the process.

    Same contract as :func:`write_text_or_die` (#1641).
    """
    try:
        write_json_secure(path, data, indent=indent, ensure_ascii=ensure_ascii, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


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
    exit_code: int = EXIT_CALLER_ERROR,
) -> None:
    """:func:`ensure_dir`, but an unusable path ends the process (rc=2)."""
    try:
        ensure_dir(path, flag=flag)
    except OutputWriteError as exc:
        _die_on_write_error(exc, exit_code)


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
    turning it into this message would misattribute it. Tools that must emit
    a ``--json`` envelope on stdout for this path catch the error themselves
    instead of using this.
    """
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except OutputWriteError as exc:
            _die_on_write_error(exc, EXIT_CALLER_ERROR)
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
