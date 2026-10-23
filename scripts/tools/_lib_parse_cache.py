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
import contextvars
import hashlib
import io
import os
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

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

#: The cache-key mode of a carrier verdict (`ConfdReader._verdict`).
VERDICT_MODE = "parse-tree:carrier-verdict(not_read, defaults_carriers)"


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

    *binary* / *timeout* are the da-guard to run and its limit (None: the
    tool's selection, `_lib_parse_tree.use_da_guard`, else
    ``$DA_GUARD_BINARY``, then ``$PATH``, and the default timeout). The
    tree-mode document is run at most once per reader, and so is the
    listing (`listing`)."""

    def __init__(self, root: "str | os.PathLike[str]", *, binary: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self.root = Path(root)
        self.binary = binary
        self.timeout = timeout
        self._tree: Optional[Tree] = None
        self._listing: "Optional[_lib_parse_tree.Listing]" = None
        self._verdicts: "dict[str, Optional[tuple[Optional[str], dict]]]" = {}
        self._carriers_by_dir: "Optional[dict[str, list[str]]]" = None

    def tree(self) -> Tree:
        """The tree-mode document (run on first use). Raises ParseTreeError.

        ⛔ THE fail-closed exit of a tool run (#3206): with no cache
        installed and outside `judging()`, a document with any entry Go
        gives a `verdict` ends the process here — every such path named,
        exit 2 (`_lib_parse_tree.refuse_unservable`). Every accessor of the
        run's index (`listing`, `entry`, `read`, `node`, `not_read`,
        `carrier`, `require`, `tenant_scope`, `declared_tenants`,
        `_lib_confd.config_listing`, the served-values loaders) goes through
        here, so a tool cannot take a file list or a tenant set from a tree
        it may not serve. A tool that writes calls `require()` before its
        first write."""
        if self._tree is None:
            self._tree = _lib_parse_tree.load(self.root, self.binary, self.timeout)
        if refuses():
            _lib_parse_tree.refuse_unservable(self._tree.entries, self.root)
            _SERVABLE.add(os.path.realpath(self.root))
        return self._tree

    def listing(self) -> "_lib_parse_tree.Listing":
        """Which paths the tree holds and what each one is (Go's `role`), as
        ONE snapshot per reader: with no cache, the entries of the tree-mode
        document this reader reads the content from (`listing_of` — no
        second walk); under a cache (the generator container, which never
        runs tree mode), one `parse-tree --list-only` run, kept. Raises
        ParseTreeError."""
        if self._listing is None:
            if _ACTIVE is None:
                self._listing = _lib_parse_tree.listing_of(self.tree(), self.root)
            else:
                self._listing = _lib_parse_tree.list_tree(self.root, self.binary, self.timeout)
        return self._listing

    def entry(self, path: "str | os.PathLike[str]") -> "Optional[_lib_parse_tree.Entry]":
        """The `listing` entry of *path*, or None when the walk does not list
        it (no such file, a hidden or non-config name, outside the root)."""
        return self.listing().entry(self.rel(path))

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
        """Why no reader reads *path*, as parse-tree says (its `not_read`
        entry: `unselected_defaults_carrier` for the `_defaults` spelling a
        directory does not select), or None when some reader reads it —
        `selected` (an unreadable file included: `node` names its kind) — or
        parse-tree does not list it at all.

        ⛔ THE answer to "skip or read" for a tool that walks the tree itself
        (`_lib_confd.iter_config_files` / `list_config_tree`): never a
        carrier selection of its own (`_lib_confd`'s `select_defaults_carrier`
        and `readable_carriers` were deleted for this, #3206). Such a second
        implementation of the exporter's rule — a file it reads and
        parse-tree lists here makes `read` raise `NotSelected` — rc 2 for a
        valid tree (#2906 review, twice). A file listed here is skipped and
        named; a file in neither list is a walk that disagrees with the
        exporter's (`node` raises `NotSelected`). See `split_not_read`.

        With no cache this is the tree-mode document (ParseTreeError). Under
        a cache (the generator container, which never runs tree mode) it is
        parse-tree's file-mode document of the file on disk (`_verdict`, kept
        in the cache),
        asked only for a defaults-named file in a directory holding two or
        more: tree mode's one reason (parse_tree.go `notReadUnselectedCarrier`)
        is a carrier its directory's chain passes over, and a chain with one
        candidate passes over none."""
        rel = self.rel(path)
        if _ACTIVE is None:
            return self.tree().not_read.get(rel)
        if len(self._carrier_names(rel)) < 2:
            return None
        verdict = self._verdict(rel)
        return None if verdict is None else verdict[0]

    def split_not_read(self, paths: "Iterable[str | os.PathLike[str]]"
                       ) -> "tuple[list[Path], list[tuple[Path, str]]]":
        """*paths* (a walk of this tree, in its order) split by `not_read`:
        ``(read, [(path, reason), ...])``. *read* is every path some reader
        reads (or parse-tree does not list: `read` raises `NotSelected` for
        it); the second list is what the caller must skip and name."""
        read: list[Path] = []
        skipped: list[tuple[Path, str]] = []
        for p in paths:
            why = self.not_read(p)
            if why is None:
                read.append(Path(p))
            else:
                skipped.append((Path(p), why))
        return read, skipped

    def carrier(self, directory: "str | os.PathLike[str]") -> Optional[Path]:
        """The defaults carrier parse-tree says *directory*'s chain selects
        (its `defaults_carriers`), as a path under the root, or None when the
        directory has none — never a Python selection's answer (see
        `not_read`). Under a cache: the file-mode document of a
        defaults-named file in the directory. Raises ParseTreeError."""
        d = self.rel(directory)
        if _ACTIVE is None:
            chosen = defaults_carriers(self.tree()).get(d)
        else:
            chosen = None
            for name in self._carrier_names("_" if d == "." else d + "/_"):
                verdict = self._verdict(name if d == "." else f"{d}/{name}")
                if verdict is not None:
                    chosen = verdict[1].get(d)
                    break
        return None if chosen is None else self.root / chosen

    def _carrier_names(self, rel: str) -> list[str]:
        """The defaults carriers (class `defaults`, role `config`: the
        exporter's `confdname.IsDefaults`, a file its walk reaches — an
        unreadable one included, `_verdict` keys it as such) the reader's
        `listing` holds in *rel*'s directory, by name, sorted; [] unless
        *rel* itself is one or names no file (``dir/_``)."""
        parent, _, base = rel.rpartition("/")
        if self._carriers_by_dir is None:
            by_dir: dict[str, list[str]] = {}
            for e in self.listing().entries:
                if e.cls == "defaults" and e.config_file:
                    d, _, name = e.path.rpartition("/")
                    by_dir.setdefault(d, []).append(name)
            self._carriers_by_dir = by_dir
        names = sorted(self._carriers_by_dir.get(parent, []))
        return names if base == "_" or base in names else []

    def _verdict(self, rel: str) -> "Optional[tuple[Optional[str], dict]]":
        """Under a cache: ``(not_read reason or None, defaults_carriers)`` of
        parse-tree's file-mode document of *rel* as it is on disk, or None
        when *rel* cannot be opened (parse-tree names it as unreadable on
        `read`).

        Kept in the cache under :data:`VERDICT_MODE`, keyed by *rel* and the
        name and bytes of every defaults carrier in its directory — what the
        selection reads (`SelectDefaultsCarriers` over the carriers the walk
        could read): a judgement that changes none of them runs no parse-tree
        for it. A carrier that cannot be opened enters the key as such."""
        if rel in self._verdicts:
            return self._verdicts[rel]
        parent = rel.rpartition("/")[0]
        h = hashlib.sha256()
        data = None
        for name in self._carrier_names(rel):
            sib = f"{parent}/{name}" if parent else name
            try:
                with open(os.path.join(self.root, sib), "rb") as fh:
                    content = fh.read()
            except OSError:
                content = None
            if sib == rel:
                data = content
            n = sib.encode("utf-8", "surrogateescape")
            h.update(len(n).to_bytes(8, "big") + n)
            h.update(b"\x00" if content is None
                     else b"\x01" + len(content).to_bytes(8, "big") + content)
        if data is None:
            self._verdicts[rel] = None
            return None
        key = (content_key(rel, h.digest()), VERDICT_MODE)
        cache = _ACTIVE
        value = cache.cached(key) if cache is not None else MISS
        if value is MISS:
            if cache is not None:
                cache.parses += 1  # a parse-tree run, as a read's miss
            tree = _lib_parse_tree.load_file(self.root, rel, data, self.binary, self.timeout)
            value = (tree.not_read.get(rel), dict(defaults_carriers(tree)))
            if cache is not None:
                cache.store(key, value)
        self._verdicts[rel] = value
        return value

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


# ── who may read a tree Go names paths on (#3206) ───────────────────────

_JUDGING: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "_lib_parse_cache._JUDGING", default=False)


@contextlib.contextmanager
def judging() -> Iterator[None]:
    """For the length of the block, `ConfdReader.tree` does not refuse a
    tree Go names paths on: the block's verdict on the tree IS the run's
    output (`validate_config`, the route generator: each names every such
    path as its own finding and fails). ⛔ Nothing else enters it; a test
    pins who does (`test_confd_tools_no_python_judgement`). Under a cache
    (the generator container) the reader never refuses."""
    token = _JUDGING.set(True)
    try:
        yield
    finally:
        _JUDGING.reset(token)


def refuses() -> bool:
    """Whether a reader of this run refuses a tree Go names paths on: no
    cache installed (the CLI) and outside `judging()`."""
    return _ACTIVE is None and not _JUDGING.get()


#: The roots (realpath) a refusing reader of this process has read whole
#: and found servable: the run's index of that tree, already taken.
_SERVABLE: "set[str]" = set()


def require_servable(root: "str | os.PathLike[str]", *, binary: Optional[str] = None,
                     timeout: Optional[float] = None) -> None:
    """For a read of *root* that is not through a reader (the served-values
    family): the run's fail-closed exit (`ConfdReader.tree`), once per tree
    and process — a tree a reader of this run already read is not read
    again; else the run's reader of it (`reader_for`) reads it now, with
    *binary* / *timeout*. A no-op where readers do not refuse
    (`refuses`). Raises ParseTreeError."""
    if not refuses() or os.path.realpath(root) in _SERVABLE:
        return
    with _lib_parse_tree.use_da_guard(binary, timeout):
        reader_for(root).require()


def judge_now(root: "str | os.PathLike[str]", *, binary: Optional[str] = None,
              timeout: Optional[float] = None) -> "tuple[Tree, list[str]]":
    """``(tree-mode document, refusal lines)`` of *root* as Go judges it NOW
    — the lines are [] when no entry has a verdict — for a long-running
    writer (the CRD controller, `da_assembler`) that must judge the tree
    afresh every cycle, go on running when it is refused, and may take
    what else it needs of the tree (its entries) from the same document.
    ⛔ The explicit bypass of `require_servable`'s once-per-process memo:
    one tree-mode run every call, nothing kept in `_SERVABLE`, never exits;
    the caller names every line and skips its writes. Raises
    ParseTreeError."""
    tree = _lib_parse_tree.load(root, binary, timeout)
    bad = _lib_parse_tree.refused_entries(tree.entries)
    return tree, (_lib_parse_tree.refusal_lines(bad, root) if bad else [])


def refusal_now(root: "str | os.PathLike[str]", *, binary: Optional[str] = None,
                timeout: Optional[float] = None) -> "list[str]":
    """`judge_now`'s refusal lines alone ([] = Go serves *root*). Raises
    ParseTreeError."""
    return judge_now(root, binary=binary, timeout=timeout)[1]


def subtree_reader(subtree: "str | os.PathLike[str]", root: "str | os.PathLike[str]", *,
                   fresh: bool = False) -> ConfdReader:
    """The reader of *subtree*, a directory the walk enters below conf.d root
    *root*, built from the ROOT's tree-mode document (#3206): the run's
    fail-closed exit is taken on the whole tree (`ConfdReader.tree` of
    *root*), and the subtree's paths keep Go's verdicts as they are where
    they really are (`_lib_parse_tree.subtree_of`) — never a parse-tree run
    on the subtree, which would judge it as a root. *fresh*: new readers
    (a re-read after writes) instead of the session's. Raises ValueError when
    *subtree* is not a directory the walk enters under *root*, and
    ParseTreeError as `tree` does."""
    root_reader = ConfdReader(root) if fresh else reader_for(root)
    tree = root_reader.tree()
    rel = Path(os.path.relpath(os.path.realpath(subtree), os.path.realpath(root))).as_posix()
    entry = next((e for e in tree.entries if e.path == rel), None)
    if rel == "." or rel.startswith("../") or entry is None or not entry.is_dir:
        raise ValueError(f"{subtree} is not a directory the exporter's walk enters under {root}")
    reader = ConfdReader(subtree) if fresh else reader_for(subtree)
    if reader._tree is None:
        reader._tree = _lib_parse_tree.subtree_of(tree, rel)
    _SERVABLE.add(os.path.realpath(subtree))
    return reader


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


# ── parse-tree's file selection, for the read-only tools (#2906 E1) ─────
#
# A tool that asks "which files are there" asks the same document it reads
# the content from: parse-tree's `selected`, `defaults_carriers` and
# `not_read` — never a Python walk of the directory, which is a second
# answer to the exporter's own (ADR-036 §2).

def defaults_carriers(tree: Tree) -> "Mapping[str, str]":
    """Directory ("." for the root) → the one defaults carrier its chain
    reads, as parse-tree's `defaults_carriers` says. A directory with no
    carrier is absent. Raises ParseTreeError for a document without the
    field (a da-guard older than this tool)."""
    if tree.defaults_carriers is None:
        raise ParseTreeError(
            f"da-guard {_lib_parse_tree.SUBCOMMAND} wrote no \"defaults_carriers\": this "
            "da-guard is older than this tool; upgrade or rebuild it")
    return tree.defaults_carriers


def root_defaults_carrier(tree: Tree) -> Optional[str]:
    """The root's defaults carrier (root-relative), or None when the root has
    none. Raises as :func:`defaults_carriers`."""
    return defaults_carriers(tree).get(".")


def nested_defaults_carriers(tree: Tree) -> list[str]:
    """Every defaults carrier below the root (sorted, root-relative)."""
    return sorted(p for d, p in defaults_carriers(tree).items() if d != ".")


def tenant_files(tree: Tree) -> list[str]:
    """The tenant files of an in-memory document — a ConfigMap's keys
    (`patch_config`), where no directory, link or special file can exist,
    so `tenant_scope(...).unknown` is empty by construction — and of the
    readers that name every flagged entry themselves from the same document
    (`gitops_check`, `check_path_metadata_consistency`). ⛔ Any other reader
    of the tenant set takes :func:`tenant_scope` (both halves); a test pins
    who calls this (`test_confd_tools_no_python_judgement`)."""
    return list(_lib_parse_tree.tenant_file_paths(tree.entries))


def tenant_scope(reader: ConfdReader, *, exclude: "Callable[[str], bool] | None" = None
                 ) -> "_lib_parse_tree.TenantScope":
    """*reader*'s `_lib_parse_tree.TenantScope` — the tenant files and the
    entries that leave the set unknown — from its one listing. Raises
    ParseTreeError."""
    return _lib_parse_tree.tenant_scope(reader.listing().entries,
                                        getattr(reader, "binary", None),
                                        getattr(reader, "timeout", None), exclude=exclude)


def declared_tenants(reader: ConfdReader, *, exclude: "Callable[[str], bool] | None" = None
                     ) -> "tuple[dict[str, list[str]], list[FileUnreadable]]":
    """``({tenant id: [file, ...]}, unknown)``: the keys of the
    mapping-shaped ``tenants:`` of each tenant file (:func:`tenant_scope`;
    *exclude* as there), keyed by their source text as yaml.v3 keys the
    exporter's ``map[string]``. *unknown* is BOTH halves of what leaves the
    answer open, one list a caller cannot take half of: each entry
    `tenant_scope` names as possibly hiding a tenant file (a directory Go
    could not list, a link it does not follow, …), then each tenant file
    parse-tree cannot read — its content, and so what it declares, is
    unknown — but a file whose top level is not a mapping, which declares
    nothing. The caller names them. Raises ParseTreeError / NotSelected."""
    scope = tenant_scope(reader, exclude=exclude)
    declared: dict[str, list[str]] = {}
    unknown: list[FileUnreadable] = list(scope.unknown)
    for rel in scope.files:
        try:
            data = reader.read(rel)
        except FileUnreadable as exc:
            if exc.kind != "top_level_not_mapping":
                unknown.append(exc)
            continue
        tenants = data.get("tenants")
        if isinstance(tenants, dict):
            for tenant in tenants:
                declared.setdefault(tenant, []).append(rel)
    return declared, unknown


def read_text_as(root: "str | os.PathLike[str]", rel: str, data: bytes,
                 mode: ParseMode = VALUES, *, binary: Optional[str] = None,
                 timeout: Optional[float] = None) -> Any:
    """*data* read as the file at *rel* (root-relative, '/') of the conf.d at
    *root* — parse-tree's file mode, for content that is not on disk (a
    `git show` of an earlier commit). Raises FileUnreadable (its ``file`` is
    *rel*) and ParseTreeError."""
    tree = _lib_parse_tree.load_file(root, rel, data, binary, timeout)
    return mode.convert(tree.node(rel))


def read_file_alone(path: "str | os.PathLike[str]", mode: ParseMode = VALUES, *,
                    binary: Optional[str] = None,
                    timeout: Optional[float] = None) -> Any:
    """A YAML file that belongs to no conf.d tree (a standalone policy file
    named on the command line) as yaml.v3 reads it: parse-tree's file mode,
    its bytes judged in an empty directory, so no tree is walked. Raises
    OSError (the file cannot be read), FileUnreadable (``file`` is *path*)
    and ParseTreeError."""
    import tempfile
    p = Path(path)
    data = p.read_bytes()
    with tempfile.TemporaryDirectory(prefix="da-parse-") as empty:
        try:
            # A fixed neutral name, not p.name: the content is judged the
            # same under any name, and a name that is not UTF-8 (which JSON
            # cannot carry as --path) must not refuse a readable file.
            return read_text_as(empty, "standalone.yaml", data, mode,
                                binary=binary, timeout=timeout)
        except FileUnreadable as exc:
            raise FileUnreadable(str(path), exc.kind, exc.message) from None


def parse_tree_error_line(exc: BaseException) -> str:
    """The one `ERROR:` line for no usable `da-guard parse-tree` (no binary,
    a failing one, one older than this tool): its reason on one line, every
    character a terminal could act on escaped."""
    from _lib_io import safe_label  # lazy: _lib_io imports this module lazily too
    return (f"ERROR: cannot read the conf.d through `da-guard parse-tree` (ADR-036): "
            f"{safe_label(' '.join(str(exc).split()))}")


def exit_on_parse_tree_error(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate a CLI `main`: no usable `da-guard parse-tree` (a
    `ParseTreeError` that is not a `FileUnreadable` — a file it cannot read
    is the tool's own finding) exits 2 (`EXIT_CALLER_ERROR`) with
    :func:`parse_tree_error_line` on stderr — the tree was not read, so
    there is no verdict."""
    import functools
    import sys

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except FileUnreadable:
            raise
        except ParseTreeError as exc:
            from _lib_exitcodes import EXIT_CALLER_ERROR
            print(parse_tree_error_line(exc), file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    return _wrapped


def warn_skipped_nested_carriers(tree: Tree, *, tool: str, note: str) -> None:
    """For a reader that takes something from the ROOT defaults carrier only
    (`_policies`, OPA's `input.defaults`): one stderr line naming the
    defaults carriers below the root that parse-tree selects and the reader
    does not read, behind *note* — the `resolve_defaults_file(skipped_note=)`
    line, from parse-tree's `defaults_carriers`. Nothing for a flat tree."""
    import sys
    from _lib_confd import WARN_LIMIT, printable_name
    missed = nested_defaults_carriers(tree)
    if not missed:
        return
    shown = ", ".join(printable_name(p) for p in missed[:WARN_LIMIT])
    more = f" (+{len(missed) - WARN_LIMIT} more)" if len(missed) > WARN_LIMIT else ""
    print(f"WARN: {tool}: {note} {len(missed)} defaults file(s) in subdirectories: "
          f"{shown}{more}", file=sys.stderr)
