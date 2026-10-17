"""_lib_parse_cache.py — the conf.d reader of the routing tools, with a cache.

ADR-036 step 4 (#2906): the route generator, and every tool that reads a
conf.d through it, reads a conf.d file's content from `da-guard parse-tree`
(`_lib_parse_tree`) — yaml.v3 is the only YAML parser of conf.d — never with
PyYAML. This module is that read, by path and by tree:

* :class:`ConfdReader` is one conf.d tree. ``read(path, mode)`` is one file
  of it as Python values, converted from parse-tree's tagged nodes by a
  :class:`ParseMode` (:data:`VALUES`, or :data:`TENANT_LISTS_AS_TEXT` for the
  routing reader: a domain policy's ``tenants:`` list as source text). With
  no cache installed (every CLI run) the reader runs ``parse-tree`` once, in
  tree mode, and answers every file from that one document — so a run reads
  one snapshot of the tree, whatever the tool asks.
* A file parse-tree cannot read raises
  :class:`_lib_parse_tree.FileUnreadable` (its ``kind`` is the closed-list
  code); one parse-tree does not select raises :class:`NotSelected` (the
  walks disagree — never an empty mapping); no binary, a failing binary or
  a document that cannot be read raises :class:`_lib_parse_tree.ParseTreeError`
  (:class:`~_lib_parse_tree.BinaryMissing` for no binary). Callers give
  those last two exit 2, as for any missing or failing da-guard.

⛔ The older stream readers (``_lib_io.strict_load_exporter_keys`` and
friends) are untouched: they have callers that read files that are not
conf.d (the state store, admin config, the metric dictionary). Nothing on
the routing tools' conf.d path calls them.

The generator container (``ops/generator_service``) installs a
:class:`ParseCache` in each judgement process. Then a read is: the file's
bytes → :func:`content_key` of its path and content → look up
``(key, mode name)`` → on a miss, ``parse-tree`` in file mode on those bytes
(``--path <rel> --input -``) and keep a pickled copy of the result.
Every miss is file mode: the key is computed from the bytes this process
read, and the value stored under it is the parse of those same bytes (fed
on stdin), never a tree-mode document — parse-tree would read the file
again itself, and the file may have changed in between. A changed file can
thus never be answered with an older or a newer file's result; the mode is
in the key because one content read two ways gives two results.

⛔ SHA-256, whatever the repository's object format — never the git blob id.
In a SHA-1 repository a blob id can be collided (chosen prefix), and two
contents with one key would let the parse of one be served for the other: a
write could be judged on content it does not contain.

⛔ A cached value is pickled bytes, and it is read back with
:func:`restricted_loads`, whose ``find_class`` admits only the types a
parse-tree conversion produces (:data:`ALLOWED_GLOBALS`). The bytes come
from a process that read an attacker's YAML; a value that does not load
under that rule counts as a miss, never as data.

A read that raises is never cached: the caller sees the exception exactly as
it would without a cache. A ``MemoryError`` is recorded on the cache
(:attr:`ParseCache.memory_error`) before it propagates, because the
generator's own read loop turns a file's failure into a finding — the
judgement process still has to know that the reason was the memory limit
(ADR-036 §3 "拒收該筆").
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

import _lib_parse_tree
from _lib_parse_tree import FileUnreadable, ParseTreeError, Tree  # noqa: F401 (re-exported)

#: The only globals a cached value may name. A parse-tree conversion yields
#: dict / list / str / int / float / bool / None, which pickle protocol 5
#: writes with opcodes of their own, and ``!!timestamp`` values as
#: ``_lib_parse_tree.Timestamp``. Nothing else is admitted.
ALLOWED_GLOBALS = frozenset({
    ("_lib_parse_tree", "Timestamp"),
})

PICKLE_PROTOCOL = 5

#: How long one ``parse-tree`` run may take (seconds), as the other
#: da-guard readers (``_lib_tenant_values.DEFAULT_TIMEOUT``).
DEFAULT_TIMEOUT = 600

class NotSelected(ParseTreeError):
    """A file this tool reads is not one parse-tree selects: the Python walk
    and the exporter's walk disagree about it. Nothing of the tree is used."""


@dataclass(frozen=True)
class ParseMode:
    """One way of converting a file's parse-tree content to Python values:
    *name* is the cache key's second half and must spell out the conversion
    (two modes with the same name share entries); *convert* takes the
    file's tagged nodes."""

    name: str
    convert: Callable[[Any], Any] = field(compare=False)


#: Every value by its yaml.v3 tag (`_lib_parse_tree.to_python`).
VALUES = ParseMode("parse-tree:to_python", _lib_parse_tree.to_python)

#: The routing reader's mode: as VALUES, and every scalar item of a sequence
#: under a `tenants` key as its source text — a domain policy's `tenants:`
#: list meets the tenant ids, which are keys and so always text.
TENANT_LISTS_AS_TEXT = ParseMode(
    "parse-tree:to_python(text_sequences=('tenants',))",
    lambda node: _lib_parse_tree.to_python_text_sequences(node, ("tenants",)))


def content_key(rel: str, data: bytes) -> str:
    """The cache key's first half: SHA-256 (hex) of the root-relative path
    and the content — length-prefixed, so no (path, content) pair spells
    another's."""
    p = rel.encode("utf-8", "surrogateescape")
    return hashlib.sha256(len(p).to_bytes(8, "big") + p + data).hexdigest()


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:  # noqa: D102
        if (module, name) in ALLOWED_GLOBALS:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"global {module}.{name} is not admitted")


def restricted_loads(raw: bytes) -> Any:
    """*raw* unpickled with only :data:`ALLOWED_GLOBALS`; raises otherwise."""
    return _RestrictedUnpickler(io.BytesIO(raw)).load()


class ParseCache:
    """The cache one judgement process reads and fills.

    *entries* maps ``(content_key, mode name)`` to pickled bytes and is only
    read (in the generator container it is the long-running process's
    store, inherited by fork). Entries this process adds go to :attr:`new`,
    and each one is also handed to *on_store* ``(key, pickled bytes)`` the
    moment it exists — the generator container streams it to the
    long-running process, so a judgement that is killed later still leaves
    the reads it finished behind (#2901).
    """

    def __init__(self, entries: "Mapping[tuple[str, str], bytes] | None" = None,
                 on_store: "Callable[[tuple[str, str], bytes], None] | None" = None
                 ) -> None:
        self.entries: Mapping[tuple[str, str], bytes] = entries or {}
        self.new: dict[tuple[str, str], bytes] = {}
        self.hits: dict[tuple[str, str], None] = {}  # ordered set
        self.parses = 0
        self.rejected_entries = 0
        self.memory_error = False
        self.on_store = on_store

    def cached(self, key: tuple[str, str]) -> Any:
        """The value stored for *key*, or :data:`MISS`."""
        raw = self.new.get(key)
        if raw is None:
            raw = self.entries.get(key)
        if raw is None:
            return MISS
        try:
            value = restricted_loads(raw)
        except Exception:  # noqa: BLE001 — anything unreadable is a miss
            self.rejected_entries += 1
            return MISS
        self.hits[key] = None
        return value

    def store(self, key: tuple[str, str], value: Any) -> None:
        """Keep *value* (pickled now, before the caller can change it)."""
        try:
            raw = pickle.dumps(value, protocol=PICKLE_PROTOCOL)
        except MemoryError:
            self.memory_error = True
            raise
        except Exception:  # noqa: BLE001 — e.g. RecursionError: just not kept
            return
        self.new[key] = raw
        if self.on_store is not None:
            self.on_store(key, raw)


MISS = object()
_ACTIVE: Optional[ParseCache] = None


def install(cache: Optional[ParseCache]) -> Optional[ParseCache]:
    """Make *cache* the one every :class:`ConfdReader` uses (None removes
    it); returns the one it replaces."""
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, cache
    return previous


def active() -> Optional[ParseCache]:
    """The installed cache, or None (the CLI)."""
    return _ACTIVE


class ConfdReader:
    """One conf.d tree, read through ``da-guard parse-tree``, by path.

    *binary* is the da-guard to run (None: ``$DA_GUARD_BINARY``, then
    ``$PATH`` — `guard_dispatch`'s resolution). The tree-mode document is
    run at most once per reader."""

    def __init__(self, root: "str | os.PathLike[str]", *, binary: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.root = Path(root)
        self.binary = binary
        self.timeout = timeout
        self._tree: Optional[Tree] = None

    def tree(self) -> Tree:
        """The tree-mode document (run on first use). Raises ParseTreeError."""
        if self._tree is None:
            self._tree = _lib_parse_tree.load(self.root, self.binary, self.timeout)
        return self._tree

    def require(self) -> None:
        """Run parse-tree now unless a cache answers reads lazily — so a run
        without a usable da-guard fails the same way on every tree, an empty
        one too. Raises ParseTreeError."""
        if _ACTIVE is None:
            self.tree()

    def rel(self, path: "str | os.PathLike[str]") -> str:
        """*path* relative to the root, with '/' (a relative *path* is taken
        as relative to the root already)."""
        p = Path(path)
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            pass
        if not p.is_absolute():
            return p.as_posix()
        return Path(os.path.relpath(p, os.path.abspath(self.root))).as_posix()

    def _node(self, tree: Tree, rel: str) -> Any:
        try:
            return tree.node(rel)
        except KeyError as e:
            raise NotSelected(
                f"{rel}: da-guard parse-tree does not read this file ({e.args[0]}) — "
                "this tool's file walk and the exporter's disagree; nothing of the tree "
                "is used") from None

    def not_read(self, path: "str | os.PathLike[str]") -> Optional[str]:
        """Why no reader reads *path*, as parse-tree's tree-mode document says
        (its `not_read` entry: e.g. `unselected_defaults_carrier` for the
        `_defaults` spelling a directory does not select), or None when some
        reader reads it — `selected` (an unreadable file included: `node`
        names its kind) — or parse-tree does not list it at all.

        A tool that walks the directory itself asks this before `node`: a
        file listed here is parse-tree's verdict that nothing reads it (skip
        it and say so), while a file in neither list is a walk that disagrees
        with the exporter's (`node` raises `NotSelected`). Runs the tree-mode
        document (ParseTreeError)."""
        return self.tree().not_read.get(self.rel(path))

    def node(self, path: "str | os.PathLike[str]") -> Any:
        """The file's tagged nodes, from the tree-mode document (shared: do
        not mutate). Raises FileUnreadable / NotSelected / ParseTreeError."""
        return self._node(self.tree(), self.rel(path))

    def read(self, path: "str | os.PathLike[str]", mode: ParseMode = VALUES) -> Any:
        """The file at *path* as Python values, converted by *mode*: through
        the installed cache when there is one. Raises FileUnreadable when
        parse-tree cannot read it, NotSelected, ParseTreeError."""
        rel = self.rel(path)
        cache = _ACTIVE
        if cache is None:
            return mode.convert(self._node(self.tree(), rel))
        try:
            with open(os.path.join(self.root, rel), "rb") as fh:
                data = fh.read()
        except OSError:
            # parse-tree names why (read_failure, not_regular_file, …).
            return mode.convert(self._node(self.tree(), rel))
        key = (content_key(rel, data), mode.name)
        value = cache.cached(key)
        if value is not MISS:
            return value
        cache.parses += 1
        # Always file mode on `data`, the very bytes the key was computed
        # from (fed on stdin): the stored value can only be their parse. A
        # tree-mode document would be parse-tree's own, later read of the
        # file — it may have changed in between, and its value under this
        # key would be the parse of other content.
        try:
            tree = _lib_parse_tree.load_file(self.root, rel, data, self.binary, self.timeout)
            value = mode.convert(self._node(tree, rel))
        except MemoryError:
            cache.memory_error = True
            raise
        cache.store(key, value)
        return value


# ── one reader per tree for the length of a run ─────────────────────────

_SESSION: "Optional[dict[str, ConfdReader]]" = None


@contextlib.contextmanager
def session() -> Iterator[None]:
    """For the length of the block, :func:`reader_for` gives one reader per
    conf.d root, so every check of one run reads one parse-tree document
    (one snapshot of the tree). Outside a session each call is a new
    reader: a library caller never sees a document older than its call."""
    global _SESSION
    outer, _SESSION = _SESSION, {}
    try:
        yield
    finally:
        _SESSION = outer


def reader_for(root: "str | os.PathLike[str]") -> ConfdReader:
    """The reader of *root*: the session's (see :func:`session`) or a new one."""
    if _SESSION is None:
        return ConfdReader(root)
    key = os.path.realpath(root)
    reader = _SESSION.get(key)
    if reader is None:
        reader = _SESSION[key] = ConfdReader(root)
    return reader
