"""_lib_parse_cache.py — conf.d parse entry point with a content-addressed cache.

The one way the routing generator turns a conf.d file's bytes into Python
objects, with an optional content-addressed cache (ADR-036 §3, #2901).

Every place on the ``generate-routes --validate --strict`` path that parses a
conf.d file calls :func:`read_confd_yaml` with a :class:`ParseMode` — which
parser, with which arguments. With no cache installed (every CLI run) that is
exactly the read the call site did before: ``open(path, encoding="utf-8")``
and the parser on that stream, so a run's output does not change by a byte.

The generator container (``ops/generator_service``) installs a
:class:`ParseCache` in each judgement process. Then a read is: the file's
bytes → their git blob id (the repo's object format, sha1 or sha256) → look up
``(blob id, mode name)`` → on a miss, parse as above and keep a pickled copy
of the result. The key is computed from the content, so a changed file can
never be answered with an older file's result; the mode is in the key because
one content read by two parsers gives two different results.

⛔ A cached value is pickled bytes, and it is read back with
:func:`restricted_loads`, whose ``find_class`` admits only the types a
SafeLoader-based parser produces (:data:`ALLOWED_GLOBALS`). The bytes come
from a process that parsed an attacker's YAML; a value that does not load
under that rule counts as a miss, never as data.

A parse that raises is never cached: the caller sees the exception exactly as
it would without a cache. A ``MemoryError`` raised by a parse is recorded on
the cache (:attr:`ParseCache.memory_error`) before it propagates, because the
generator's own read loop catches every exception of a file and turns it into
a finding — the judgement process still has to know that the reason was the
memory limit (ADR-036 §3 "拒收該筆").

Standard library only.
"""
from __future__ import annotations

import hashlib
import io
import pickle
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

#: The object formats git supports, by ``git rev-parse --show-object-format``.
OBJECT_FORMATS = ("sha1", "sha256")

#: The only globals a cached value may name. A parser built on PyYAML's
#: SafeConstructor yields dict / list / str / int / float / bool / None /
#: bytes / set / tuple (``!!omap`` / ``!!pairs`` items), which pickle protocol
#: 5 writes with opcodes of their own, and ``!!timestamp`` values, which need
#: these four classes. Nothing else is admitted.
ALLOWED_GLOBALS = frozenset({
    ("datetime", "date"),
    ("datetime", "datetime"),
    ("datetime", "timedelta"),
    ("datetime", "timezone"),
})

PICKLE_PROTOCOL = 5


@dataclass(frozen=True)
class ParseMode:
    """One way of parsing a conf.d file: *name* is the cache key's second
    half and must spell out the parser and its arguments (two call sites
    with the same name share entries); *parse* reads an open text stream."""

    name: str
    parse: Callable[[Any], Any] = field(compare=False)


def git_blob_id(data: bytes, object_format: str = "sha1") -> str:
    """The id ``git hash-object`` gives *data* in a repo of *object_format*."""
    if object_format not in OBJECT_FORMATS:
        raise ValueError(f"unknown git object format: {object_format!r}")
    h = hashlib.new(object_format)
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:  # noqa: D102
        if (module, name) in ALLOWED_GLOBALS:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"global {module}.{name} is not admitted")


def restricted_loads(raw: bytes) -> Any:
    """*raw* unpickled with only :data:`ALLOWED_GLOBALS`; raises otherwise."""
    return _RestrictedUnpickler(io.BytesIO(raw)).load()


def _parse_file(path: Any, mode: ParseMode) -> Any:
    # ⛔ The pre-cache read, verbatim: a text-mode open (so a decode error
    # and a YAML mark carry the same offsets and file name as always).
    with open(path, encoding="utf-8") as f:
        return mode.parse(f)


class ParseCache:
    """The cache one judgement process reads and fills.

    *entries* maps ``(blob id, mode name)`` to pickled bytes and is only
    read (in the generator container it is the long-running process's
    store, inherited by fork). Entries this process adds go to :attr:`new`,
    and each one is also handed to *on_store* ``(key, pickled bytes)`` the
    moment it exists — the generator container streams it to the
    long-running process, so a judgement that is killed later still leaves
    the parses it finished behind (#2901).
    """

    def __init__(self, entries: "Mapping[tuple[str, str], bytes] | None" = None,
                 object_format: str = "sha1",
                 on_store: "Callable[[tuple[str, str], bytes], None] | None" = None
                 ) -> None:
        if object_format not in OBJECT_FORMATS:
            raise ValueError(f"unknown git object format: {object_format!r}")
        self.entries: Mapping[tuple[str, str], bytes] = entries or {}
        self.object_format = object_format
        self.new: dict[tuple[str, str], bytes] = {}
        self.hits: dict[tuple[str, str], None] = {}  # ordered set
        self.parses = 0
        self.rejected_entries = 0
        self.memory_error = False
        self.on_store = on_store

    def _cached(self, key: tuple[str, str]) -> Any:
        raw = self.new.get(key)
        if raw is None:
            raw = self.entries.get(key)
        if raw is None:
            return _MISS
        try:
            return restricted_loads(raw)
        except Exception:  # noqa: BLE001 — anything unreadable is a miss
            self.rejected_entries += 1
            return _MISS

    def load(self, path: Any, mode: ParseMode) -> Any:
        """*path* parsed by *mode*, from the cache when its content is there."""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            # The uncached read raises the same error the call site expects.
            return _parse_file(path, mode)
        key = (git_blob_id(data, self.object_format), mode.name)
        value = self._cached(key)
        if value is not _MISS:
            self.hits[key] = None
            return value
        self.parses += 1
        try:
            value = _parse_file(path, mode)
        except MemoryError:
            self.memory_error = True
            raise
        try:
            # Pickled BEFORE the caller sees the value, so the entry is the
            # parse result, never a structure the generator has since changed.
            raw = pickle.dumps(value, protocol=PICKLE_PROTOCOL)
        except MemoryError:
            self.memory_error = True
            raise
        except Exception:  # noqa: BLE001 — e.g. RecursionError: just not kept
            return value
        self.new[key] = raw
        if self.on_store is not None:
            self.on_store(key, raw)
        return value


_MISS = object()
_ACTIVE: Optional[ParseCache] = None


def install(cache: Optional[ParseCache]) -> Optional[ParseCache]:
    """Make *cache* the one :func:`read_confd_yaml` uses (None removes it);
    returns the one it replaces."""
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, cache
    return previous


def active() -> Optional[ParseCache]:
    """The installed cache, or None (the CLI)."""
    return _ACTIVE


def read_confd_yaml(path: Any, mode: ParseMode) -> Any:
    """*path* (a conf.d file) parsed by *mode*: through the installed cache
    if there is one, otherwise exactly as the call site read it before."""
    cache = _ACTIVE
    if cache is None:
        return _parse_file(path, mode)
    return cache.load(path, mode)
