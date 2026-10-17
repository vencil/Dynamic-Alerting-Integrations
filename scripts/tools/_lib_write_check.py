"""_lib_write_check.py — a conf.d writer's confirmation before it writes (ADR-036 §2).

ADR-036 §2 lets a Python tool that WRITES a conf.d file keep PyYAML for two
things only: reading the original file, and producing the text to write.
What to change and whether to change it is decided from `da-guard
parse-tree`'s document, and before a byte is written the generated text is
read back by `parse-tree` in file mode, at the path it will be written to:

* the expected content is the original file's parse result with this
  change applied (for a new file: what the tool means to write);
* the whole file's keys, structure, and every scalar's source text, tag and
  value must equal the expected content — otherwise nothing is written;
* an original file `parse-tree` cannot read, or a target path no reader
  reads (a hidden name, a `_defaults` spelling its directory does not
  select, a name that is not a config file), is refused as well.

This module is that confirmation. :func:`original` reads the original bytes
(``None`` for a new file), :func:`confirm` judges one generated text, and
:func:`confirm_all` judges every file of one run before the caller writes
any of them (a tool that writes several files writes all or none).
A refusal is :class:`WriteRefused`; no usable `da-guard` is
`_lib_parse_tree.ParseTreeError` (callers give it exit 2, as for any
missing or failing da-guard).

What "source text" means here
-----------------------------
A scalar's ``text`` in the parse-tree document is yaml.v3's ``Node.Value``:
the scalar's content as written, after the quotes and escapes are read —
`'a'`, `"a"` and `a` all have the text ``a`` (and the tag tells `"010"`,
``!!str``, from `010`, ``!!int``). A rewrite that only changes HOW an
untouched scalar is quoted (`'a'` to `a`) leaves text, tag and value equal
and passes; a rewrite that changes what it says does not — `~` or an empty
value rewritten as `null` (text ``~`` / ``""`` → ``null``), `!!float 1`
rewritten as `1.0`, an unquoted `1e3` rewritten from a string: each is a
different text or tag, and the whole file is refused (ADR-036 §2 "已知後果";
the user rewrites that spelling by hand first). Mapping key order is not
compared: the document holds every mapping's keys sorted
(`_lib_parse_tree` "KEY ORDER IS NOT THE FILE'S").

Expected content
----------------
Nodes are the parse-tree shapes (`_lib_parse_tree`): a mapping is a ``dict``
keyed by each key's text, a sequence a ``list``, a scalar ``{"text", "tag"[,
"value"]}``. :func:`scalar` and :func:`from_python` build the nodes a tool
means to write from Python values; :func:`with_change` applies a change to
a copy of the original's nodes. :class:`AnyNumber` is the one expectation
that is not a single node: a scalar the tool writes as text and means to be
read as a number (`!!int` or `!!float`) — `da_assembler`'s `defaults:`,
whose type the exporter fixes (``map[string]float64``) and whose spelling
the CR gives.
"""
from __future__ import annotations

import contextlib
import copy
import decimal
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

import _lib_parse_tree
from _lib_parse_tree import FileUnreadable, ParseTreeError  # noqa: F401 (re-exported)

#: How long one file-mode run may take (seconds).
DEFAULT_TIMEOUT = 120

#: Why a write is refused (``WriteRefused.reason``).
ORIGINAL_UNREADABLE = "original_unreadable"
NOT_READ = "path_not_read"
TEXT_UNREADABLE = "text_unreadable"
CONTENT_DIFFERS = "content_differs"
REASONS = (ORIGINAL_UNREADABLE, NOT_READ, TEXT_UNREADABLE, CONTENT_DIFFERS)


class WriteRefused(Exception):
    """A conf.d write refused before any byte of it was written. *rel* is the
    root-relative path, *reason* one of :data:`REASONS`, *detail* the reason
    for a person (for :data:`CONTENT_DIFFERS`: the first differing path, with
    the expected and the read text / tag)."""

    def __init__(self, rel: str, reason: str, detail: str) -> None:
        super().__init__(f"{rel}: refusing to write ({reason}): {detail}")
        self.rel = rel
        self.reason = reason
        self.detail = detail


class AnyNumber:
    """Expect a scalar with this *text* that yaml.v3 reads as `!!int` or
    `!!float` (its value is whatever that tag gives)."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"AnyNumber({self.text!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AnyNumber) and other.text == self.text

    __hash__ = None  # type: ignore[assignment]


_NUMBER_TAGS = ("!!int", "!!float")


def scalar(value: Any) -> dict:
    """The node a tool means when it writes the Python *value*: a ``str`` is
    the string (``!!str``, whatever quoting the dump picks), a ``bool``
    ``!!bool true``/``false``, an ``int`` ``!!int`` in decimal, ``None``
    ``!!null`` spelled `null` (what PyYAML writes), a ``float`` ``!!float``
    spelled as PyYAML's representer spells it (`80.0`, `1.0e+20`, `.inf`)
    with yaml.v3's value for that text. Any other type raises TypeError."""
    if isinstance(value, bool):
        t = "true" if value else "false"
        return {"text": t, "tag": "!!bool", "value": value}
    if isinstance(value, int):
        return {"text": str(value), "tag": "!!int", "value": str(value)}
    if isinstance(value, str):
        return {"text": value, "tag": "!!str", "value": value}
    if value is None:
        return {"text": "null", "tag": "!!null", "value": None}
    if isinstance(value, float):
        return _float_node(value)
    raise TypeError(f"no expected node for a {type(value).__name__}")


def _float_node(value: float) -> dict:
    import yaml  # the representer only: the text PyYAML writes for *value*
    text = yaml.representer.SafeRepresenter().represent_float(value).value
    if math.isnan(value) or math.isinf(value):
        # parse-tree gives no "value" for these (_lib_parse_tree._FLOAT_WORDS)
        return {"text": text, "tag": "!!float"}
    # strconv.FormatFloat(f, 'f', -1, 64): the shortest digits, no exponent
    out = format(decimal.Decimal(repr(value)), "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return {"text": text, "tag": "!!float", "value": out}


def from_python(obj: Any) -> Any:
    """Nodes for a Python structure (``dict`` with ``str`` keys, ``list``,
    scalars as :func:`scalar`; an :class:`AnyNumber` or a node already
    built is kept). A key that is not a ``str`` raises TypeError."""
    if isinstance(obj, AnyNumber) or _lib_parse_tree.is_scalar(obj):
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"mapping key {k!r} is not a str")
            out[k] = from_python(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [from_python(v) for v in obj]
    return scalar(obj)


def with_change(original_nodes: Any, change: Callable[[Any], Any]) -> Any:
    """*change* applied to a deep copy of *original_nodes* (the copy is
    passed in; *change* returns the new tree, or None to keep the mutated
    copy). The original is never touched."""
    tree = copy.deepcopy(original_nodes)
    out = change(tree)
    return tree if out is None else out


@contextlib.contextmanager
def _as_root(root: "str | os.PathLike[str]") -> Iterator[Path]:
    """*root* when it is a directory; otherwise an empty stand-in: the tree
    once a writer creates *root* holds only what the writer writes."""
    p = Path(root)
    if p.is_dir():
        yield p
        return
    with tempfile.TemporaryDirectory(prefix="da-write-check-") as empty:
        yield Path(empty)


def _readers(tree: "_lib_parse_tree.Tree", rel: str) -> "tuple[str, ...]":
    return tuple(tree.selected.get(rel, ()))


def _not_read_detail(tree: "_lib_parse_tree.Tree", rel: str) -> str:
    why = tree.not_read.get(rel)
    return (f"no reader reads this path (parse-tree: {why})" if why
            else "no reader reads this path")


def read_as(root: "str | os.PathLike[str]", rel: str, data: bytes, *,
            binary: Optional[str] = None,
            timeout: float = DEFAULT_TIMEOUT) -> "_lib_parse_tree.Tree":
    """parse-tree's file-mode document of *data* read as the file at *rel*
    of the conf.d at *root* (a missing *root* as an empty tree). Raises
    ParseTreeError."""
    with _as_root(root) as r:
        return _lib_parse_tree.load_file(r, rel, data, binary, timeout)


def original(root: "str | os.PathLike[str]", rel: str, data: "bytes | None", *,
             binary: Optional[str] = None,
             timeout: float = DEFAULT_TIMEOUT) -> Any:
    """The nodes of the file a writer is about to change: *data* (its bytes,
    the very ones the writer read; None for a file that does not exist yet,
    which returns None) read by parse-tree as the file at *rel*. Raises
    WriteRefused — :data:`ORIGINAL_UNREADABLE` when parse-tree cannot read
    it, :data:`NOT_READ` when no reader reads that path — and ParseTreeError."""
    if data is None:
        return None
    tree = read_as(root, rel, data, binary=binary, timeout=timeout)
    if not _readers(tree, rel):
        raise WriteRefused(rel, NOT_READ, _not_read_detail(tree, rel))
    try:
        return tree.node(rel)
    except FileUnreadable as exc:
        raise WriteRefused(rel, ORIGINAL_UNREADABLE,
                           f"parse-tree cannot read the original file "
                           f"({exc.kind}: {exc.message})") from None


def _path_text(path: Sequence[Any]) -> str:
    if not path:
        return "(top level)"
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + str(p))
    return out


def _describe(node: Any) -> str:
    if isinstance(node, AnyNumber):
        return f"a number (!!int or !!float) written {node.text!r}"
    if _lib_parse_tree.is_scalar(node):
        return f"{node['tag']} {node['text']!r}"
    if isinstance(node, dict):
        return "a mapping"
    if isinstance(node, list):
        return "a sequence"
    return repr(node)


def first_difference(expected: Any, got: Any) -> Optional[str]:
    """The first place *got* (parse-tree nodes) differs from *expected*, as
    one line, or None when the two are equal: keys (as sets — order is not
    the file's), sequence lengths and items, and each scalar's text, tag and
    value (its presence included)."""
    stack: list[tuple[Any, Any, tuple]] = [(expected, got, ())]
    while stack:
        e, g, path = stack.pop()
        where = _path_text(path)
        if isinstance(e, AnyNumber):
            if not (_lib_parse_tree.is_scalar(g) and g["tag"] in _NUMBER_TAGS
                    and g["text"] == e.text):
                return f"at {where}: expected {_describe(e)}, read {_describe(g)}"
            continue
        if _lib_parse_tree.is_scalar(e):
            if not _lib_parse_tree.is_scalar(g):
                return f"at {where}: expected {_describe(e)}, read {_describe(g)}"
            if (e["text"], e["tag"]) != (g["text"], g["tag"]) or \
                    ("value" in e) != ("value" in g) or e.get("value") != g.get("value"):
                return (f"at {where}: expected {_describe(e)}, read {_describe(g)}"
                        + ("" if (e["text"], e["tag"]) != (g["text"], g["tag"])
                           else f" (value {e.get('value')!r} vs {g.get('value')!r})"))
            continue
        if isinstance(e, dict):
            if not isinstance(g, dict) or _lib_parse_tree.is_scalar(g):
                return f"at {where}: expected a mapping, read {_describe(g)}"
            missing = sorted(set(e) - set(g))
            extra = sorted(set(g) - set(e))
            if missing or extra:
                return (f"at {where}: keys differ"
                        + (f"; missing {missing}" if missing else "")
                        + (f"; unexpected {extra}" if extra else ""))
            for k in sorted(e, reverse=True):
                stack.append((e[k], g[k], path + (k,)))
            continue
        if isinstance(e, list):
            if not isinstance(g, list):
                return f"at {where}: expected a sequence, read {_describe(g)}"
            if len(e) != len(g):
                return f"at {where}: expected {len(e)} items, read {len(g)}"
            for i in range(len(e) - 1, -1, -1):
                stack.append((e[i], g[i], path + (i,)))
            continue
        raise TypeError(f"not an expected node at {where}: {e!r:.80}")
    return None


def confirm(root: "str | os.PathLike[str]", rel: str, text: "str | bytes",
            expected: Any, *, binary: Optional[str] = None,
            timeout: float = DEFAULT_TIMEOUT) -> None:
    """Refuse (WriteRefused) unless *text*, read by parse-tree as the file at
    *rel* of the conf.d at *root* once written, is a file some reader reads
    (:data:`NOT_READ`), that parse-tree can read (:data:`TEXT_UNREADABLE`),
    and whose nodes equal *expected* (:data:`CONTENT_DIFFERS`). Raises
    ParseTreeError when there is no usable da-guard."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    tree = read_as(root, rel, data, binary=binary, timeout=timeout)
    if not _readers(tree, rel):
        raise WriteRefused(rel, NOT_READ, _not_read_detail(tree, rel))
    try:
        got = tree.node(rel)
    except FileUnreadable as exc:
        raise WriteRefused(rel, TEXT_UNREADABLE,
                           f"parse-tree cannot read the text this tool generated "
                           f"({exc.kind}: {exc.message})") from None
    diff = first_difference(expected, got)
    if diff is not None:
        raise WriteRefused(
            rel, CONTENT_DIFFERS,
            f"the generated text does not read back as intended {diff} — PyYAML "
            f"rewrites a spelling in this file that yaml.v3 reads differently; "
            f"quote or respell it by hand, then re-run")


class NullText(str):
    """A null to be written back spelled as it was read (`~`, `null`, empty):
    the text parse-tree gave it. `_lib_yaml_keys.RewriteDumper` writes it as
    a plain null scalar with that text."""


def _represent_null_text(dumper: Any, data: "NullText") -> Any:
    import yaml
    return yaml.ScalarNode("tag:yaml.org,2002:null", str(data), style=None)


def _register_null_text() -> None:
    from _lib_yaml_keys import RewriteDumper
    RewriteDumper.add_representer(NullText, _represent_null_text)


_register_null_text()


def keep_null_spelling(doc: Any, nodes: Any) -> Any:
    """Text production only (ADR-036 §2): in the PyYAML document *doc* a
    rewrite will dump, replace each ``None`` whose parse-tree node (*nodes*,
    the same file's) is a null scalar with :class:`NullText` of its text, so
    an untouched `key:` or `key: ~` is written back as it was instead of as
    `null` — a different source text the confirmation would refuse. Walks
    the two trees together by mapping key text and sequence index; where
    they do not line up nothing is replaced. Returns *doc*."""
    stack: list[tuple[Any, Any]] = [(doc, nodes)]
    while stack:
        d, n = stack.pop()
        if isinstance(d, dict) and isinstance(n, dict) and not _lib_parse_tree.is_scalar(n):
            pairs = [(k, n[k]) for k in d if isinstance(k, str) and k in n]
        elif isinstance(d, list) and isinstance(n, list) and len(d) == len(n):
            pairs = list(zip(range(len(d)), n))
        else:
            continue
        for k, child in pairs:
            v = d[k]
            if v is None and _lib_parse_tree.is_scalar(child) and child["tag"] == "!!null":
                d[k] = NullText(child["text"])
            elif isinstance(v, (dict, list)):
                stack.append((v, child))
    return doc


def confirm_all(root: "str | os.PathLike[str]",
                items: "Iterable[tuple[str, str | bytes, Any]]", *,
                binary: Optional[str] = None,
                timeout: float = DEFAULT_TIMEOUT) -> None:
    """:func:`confirm` every ``(rel, text, expected)`` of one run, all before
    the caller writes any; raises at the first refusal."""
    for rel, text, expected in items:
        confirm(root, rel, text, expected, binary=binary, timeout=timeout)
