"""Which files a value the caller gave selects, and the bytes read from them (#3446).

The one answer to "the caller gave this value — which files does the run
end up with, and what does it read from them". Standard library only, and
`_lib_exitcodes` (whose import installs the crash hook) is imported only
when a refusal is raised, as in `_lib_paths`.

* ``add_input(parser, flag, *, select, empty, default, absent_default)`` —
  declares an input flag; ``missing`` (#3693) says what a value the caller
  GAVE with nothing at it is (``ABSENT_REFUSED``, the default:
  ``dir_not_found``; ``ABSENT_EMPTY``: selects nothing — a diff's base side,
  where no directory is a first import). ⛔ The declaration is on the argparse action
  (``action.input_spec``, an :class:`InputSpec`), in the program: the
  da-tools image ships no tests/, so a table in a test could not say what
  the shipped parser takes. The G1 gate
  (``tests/shared/test_caller_path_manifest.py``) reads it off every parser
  and runs the rows it implies.
* ``selected(args, dest, *, lang)`` — the paths the flag's value selects.
  Refuses with :class:`_lib_exitcodes.CallerError` (rc 2): reason
  ``dir_not_found`` / ``dir_not_accessible`` (``_lib_paths.classify_path``,
  and a directory that stats but cannot be listed), ``input_empty`` (the
  declaration says an empty selection is refused).
* ``absent(args, dest)`` (#3721) — whether ``selected`` found nothing at
  the flag's path (a default under ``absent_default=ABSENT_EMPTY``, a given
  value under ``missing=ABSENT_EMPTY``). ⛔ The one answer to "was it
  there": a tool never stats a declared input itself — a second look can
  disagree with the first (config-diff's ``isdir`` read a base side removed
  after it was judged as a first import, rc 1, stderr empty), and the reader
  that opens it later fails closed on what is gone: a refusal naming the
  flag (``_lib_exitcodes.InputChanged``), never an answer from less input.
  What holds it is the fault-injection gate (``JUDGED_EVENT`` below, #3724).
* ``JUDGED_EVENT`` (#3724) — the audit event (``sys.audit``) raised when
  the judgement of a declared input is done: ``(flag, path)``, once per
  path a flag names. Python raises it to audit hooks only — none is
  installed in a shipped run, so it costs a lookup. The fault-injection
  gate (``tests/shared/test_declared_input_fault_injection.py``) takes it
  as the line after which every look at the path is the tool's own: it
  removes the input at each such look, and the run must refuse it, named,
  or answer exactly what it answers with the input in place.
* ``require_listed(flag, path, root, *, lang)`` (#3693) — a directory
  da-guard lists (``DA_GUARD_LISTS``) whose root entry it reports
  ``unlistable_dir`` is ``dir_not_accessible``: ⛔ the one wording of it,
  never read as an empty tree.
* ``require_selection(args, dest, chosen, *, wanted, lang)`` — the same
  ``empty`` judgement for a selection made elsewhere (``DA_GUARD_LISTS``:
  what da-guard's listing chose), and ``refuse_empty(label, wanted, *,
  lang)`` for a selection no ``add_input`` flag makes (#3462). ⛔ The one
  wording of "selected nothing".
* ``read_bytes(path, *, flag, lang)`` — the bytes of a selected file; a
  file that cannot be read is reason ``file_unreadable``.
* ``add_input(parser, flag, *, decode=Document(...))`` (#3445) — declares
  an input FILE whose content the tool reads, and how: ``Document(fmt,
  top)``. ``document(args, dest, *, lang)`` is that content, parsed. The
  path is judged as ``_lib_paths.require_file`` judges it (``file_not_found``
  / ``file_not_accessible``); content that is not UTF-8, not valid for its
  format, or of another top-level type than declared is reason
  ``file_unreadable``: rc 2, one line naming the flag and the path — never
  a traceback, never an empty input.
* ``decode_bytes(raw, *, fmt, name)`` — ⛔ with ``read_bytes``, the one reading
  of a caller's input file: strict UTF-8, then JSON (``json.loads``) or
  YAML (PyYAML's pure-Python ``SafeLoader``, see ``_lib_io``'s
  ``load_yaml_file``, which decodes through it) or nothing (``text``).
  Raises :class:`DocumentError` with a one-line detail.
  ``refuse_top(label, got, wanted, *, lang)`` is the one wording of a top
  level of the wrong type, for a reader that is not ``document`` (a file
  da-guard reads).

PyYAML is imported inside ``decode_bytes``' YAML branch only: this module stays
importable with the standard library alone. (Not named ``decode``: that is
``add_input``'s keyword, and the YAML-reader gate,
``tests/shared/test_yaml_reader_boundary.py``, reads names — a module-level
reader named ``decode`` made every ``add_input`` call a YAML read.)

⛔ ``_list(directory)`` is the one listing of a directory in the Python
tools — ``_lib_consumer_read``'s walk and rule-source declarations ask it
too. A listing that fails raises, never answers "empty": a directory that
stats but cannot be listed (mode 0311) was read as holding nothing, and a
run made from nothing finished at rc 0. What keeps a second listing out is
``tests/shared/test_input_listing_audit.py``: the run of every declared
flag is audited, and a listing of its directory from any frame but this
module's is a fault.

A tree da-guard walks (the exporter's own walk) is listed by da-guard,
never here (``select=DA_GUARD_LISTS``): ``selected`` judges the directory itself, and
the tool refuses a listing whose root da-guard reports ``unlistable_dir``
(``require_listed``).

⛔ ``select`` (#3462) is a selector: :class:`TopLevel`, or any declaration of
which top-level names are the input — ``_lib_consumer_read.RuleSource``
(``RULE_PACKS``) is one — that answers ``choose(directory, names)`` and
``wanted``. ``selected`` lists the directory (once, ``_list``) and hands the
names over: the selector judges names, it never lists.
"""
from __future__ import annotations

import argparse
import fnmatch
import csv
import io
from datetime import datetime
import json
import os
import stat
from pathlib import Path
from typing import Any, NamedTuple, Optional

import sys

from _lib_label import safe_label
from _lib_paths import _DETAIL, _STAT_MISSING, classify_path, raise_path_problems

#: The audit event raised when a declared input's judgement is done (#3724;
#: see the module docstring): ``(flag, path)``.
JUDGED_EVENT = "da.input.judged"


def judged(flag: Optional[str], path: "str | os.PathLike[str]") -> None:
    """Say that the judgement of the input *flag* names at *path* is done
    (:data:`JUDGED_EVENT`). ⛔ Only the judgement says it: `selected`,
    `document`, and for a directory da-guard lists the code that ends its
    judgement — `_lib_parse_cache.record_judgement` for a tenant tree
    (`declared_trees`, or a tool that judges its trees itself),
    migrate-to-operator's own for its `--source-dir` (manifests)."""
    sys.audit(JUDGED_EVENT, flag, os.fspath(path))


#: ``select``: the directory is the input and da-guard lists it (the
#: exporter's walk); ``selected`` returns the directory itself.
DA_GUARD_LISTS = "da-guard"

#: ``empty``: an empty selection is a run over nothing, as the tool reads it.
EMPTY_ALLOWED = "allowed"
#: ``empty``: an empty selection is refused (``input_empty``).
EMPTY_REFUSED = "refused"
#: ``absent_default``: the default nobody gave, when nothing is at the path
#: itself (``os.lstat``: ENOENT), selects nothing. ⛔ Only absent: a default
#: that is there but no usable directory — a regular file, a dangling or
#: looping symlink, a parent that is a regular file (ENOTDIR), a stat that
#: fails — is refused like a value the caller gave (#3548: a file at
#: ``.da/state`` was read as no state and the manifest overwritten, rc 0).
ABSENT_EMPTY = "empty"
#: ``absent_default``: that default is refused like a value the caller gave.
ABSENT_REFUSED = "refused"


class TopLevel(NamedTuple):
    """``select``: the top-level entries of the directory whose name matches
    *pattern* (``fnmatch.fnmatchcase``, the whole name; a hidden name too,
    any entry type), sorted by name."""
    pattern: str

    @property
    def wanted(self) -> str:
        """What a selection looks for, as a refusal names it."""
        return self.pattern

    def choose(self, directory: Path, names: "list[str]") -> "list[Path]":
        """The entries of *names* (the sorted listing of *directory*) chosen."""
        return [directory / n for n in names if fnmatch.fnmatchcase(n, self.pattern)]


def _is_selector(select: Any) -> bool:
    return (callable(getattr(select, "choose", None))
            and isinstance(getattr(select, "wanted", None), str))


#: ``Document.fmt``: what the bytes of the file are read as.
FORMATS = ("json", "yaml", "text", "csv")

#: What a top-level type is called in a refusal, per language.
_NOUN = {
    "en": {dict: "a mapping", list: "a list", str: "a string", int: "a number",
           float: "a number", bool: "a boolean", type(None): "an empty document",
           "scalar": "a scalar"},
    "zh": {dict: "映射", list: "清單", str: "字串", int: "數字", float: "數字",
           bool: "布林值", type(None): "空文件", "scalar": "純量"},
}


def _noun(kind: Any, lang: str) -> str:
    words = _NOUN["zh" if lang == "zh" else "en"]
    return words.get(kind, words["scalar"])


def _wanted_words(top: Any, lang: str) -> str:
    tops = top if isinstance(top, tuple) else (top,)
    joiner = "或" if lang == "zh" else " or "
    return joiner.join(_noun(t, lang) for t in tops)


class Document(NamedTuple):
    """``decode``: the input is a FILE whose content the tool reads.

    *fmt* — ``"json"``, ``"yaml"``, ``"text"`` (strict UTF-8, no parse) or
    ``"csv"`` (#3662). *top* — the type the top level must be (``dict``,
    ``list``, ``str`` for text, ``list`` for csv), or a tuple of them.
    *empty_is* — YAML only: an empty document (no node, ``~``) is refused as
    of the wrong type unless a tool reads it so on purpose; then this is
    that type (``dict``: read as ``{}``), declared here, never a quiet
    ``or {}`` in the tool. *columns* — csv only, and required there: the
    header the file must have, exactly (names and order); ⛔ the producer of
    the file writes its header from the same tuple, so the two cannot
    drift. *formats* — csv only: ``(column, strptime format)`` pairs, the
    form each such column's every value must have (a timestamp). A csv
    document is its data rows as dicts (column → value); a file with no data
    row is refused — an empty report is no evidence."""
    fmt: str
    top: Any
    empty_is: Any = None
    columns: tuple = ()
    formats: tuple = ()

    @property
    def wanted(self) -> str:
        """What the top level must be, as a refusal says it ("a mapping")."""
        return _wanted_words(self.top, "en")


class DocumentError(Exception):
    """Content that ``decode_bytes`` cannot read. ``str()`` is the one-line
    detail (English); :meth:`detail` says it in a language; ``cause`` is
    the decoder's own error (``UnicodeDecodeError``, ``json.JSONDecodeError``,
    a ``yaml.YAMLError``)."""

    def __init__(self, problem: str, info: str, cause: BaseException) -> None:
        self.problem = problem   # "utf8" | "json" | "yaml" | "csv"
        self.info = info
        self.cause = cause
        super().__init__(self.detail("en"))

    def detail(self, lang: str) -> str:
        if self.problem == "utf8":
            return (f"不是有效的 UTF-8（第 {self.info} 個位元組）" if lang == "zh"
                    else f"not valid UTF-8 (byte {self.info})")
        name = self.problem.upper()
        return (f"不是有效的 {name}：{self.info}" if lang == "zh"
                else f"not valid {name}: {self.info}")


#: The detail of a document nested deeper than the parser's recursion limit.
_TOO_DEEP = "nested too deeply to read"


def decode_bytes(raw: bytes, *, fmt: str, name: str) -> Any:
    """*raw* (the bytes of the file *name*) read as *fmt*: ⛔ the one
    decoding of a caller's input file (see the module docstring). Strict
    UTF-8 first; then ``json.loads``, PyYAML's pure-Python ``SafeLoader``
    (never the C one: see the comment above ``_lib_io.load_yaml_file``) on
    a stream named *name* so its marks name the file, or nothing (text).
    Raises :class:`DocumentError`; the parser's message is one line."""
    if fmt not in FORMATS:
        raise ValueError(f"fmt must be one of {FORMATS}, not {fmt!r}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError("utf8", str(exc.start), exc) from exc
    if fmt == "text":
        return text
    if fmt == "csv":
        try:  # strict: a quote left open is an error, not a value that eats the file
            # newline=None: universal newlines (a report whose records end in a
            # bare CR is read as the text-mode open read it, #3661 review)
            return list(csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=None),
                                   strict=True))
        except csv.Error as exc:
            raise DocumentError("csv", " ".join(str(exc).split()), exc) from exc
    if fmt == "json":
        try:
            return json.loads(text)
        except RecursionError as exc:
            raise DocumentError("json", _TOO_DEEP, exc) from exc
        except ValueError as exc:  # json.JSONDecodeError is one
            raise DocumentError("json", " ".join(str(exc).split()), exc) from exc
    import yaml  # noqa: PLC0415  (lazy: the module stays standard-library only)
    stream = io.StringIO(text)
    stream.name = name
    try:
        return yaml.load(stream, Loader=yaml.SafeLoader)  # noqa: S506  (SafeLoader, pure parser)
    except RecursionError as exc:
        raise DocumentError("yaml", _TOO_DEEP, exc) from exc
    except (yaml.YAMLError, ValueError) as exc:
        # ValueError: a value SafeLoader's constructors cannot build — a
        # timestamp like 2020-99-99 — raised past PyYAML's own error class.
        raise DocumentError("yaml", " ".join(str(exc).split()) or exc.__class__.__name__,
                            exc) from exc


class InputSpec(NamedTuple):
    """What ``add_input`` declared for one flag: a directory (``select``)
    or a file whose content is read (``decode``), never both."""
    flag: str
    dest: str
    select: Any
    """A selector (:class:`TopLevel`, ``RuleSource``) or ``DA_GUARD_LISTS``;
    None for a file input."""
    empty: str
    """``EMPTY_ALLOWED`` / ``EMPTY_REFUSED``."""
    default: Optional[str]
    absent_default: Optional[str]
    """``ABSENT_EMPTY`` / ``ABSENT_REFUSED``; None when there is no default."""
    decode: Optional[Document] = None
    """The :class:`Document` of a file input; None for a directory."""
    missing: str = ABSENT_REFUSED
    """A given value with nothing at it (``_nothing_at``): ``ABSENT_REFUSED``
    (``dir_not_found``) or ``ABSENT_EMPTY`` (selects nothing), #3693."""
    separator: Optional[str] = None
    """A list of directories, one value (``a,b``), #3751: each item is one
    directory, judged on its own; None for one directory."""


class _Value(str):
    """The flag's value as the namespace holds it: the string, plus the
    declaration and whether the caller gave it (a default nobody gave is
    read by ``absent_default``)."""

    spec: InputSpec
    given: bool
    absent: Optional[bool]
    """What ``selected`` found (#3721): True when nothing was at the path and
    the declaration read it as selecting nothing; None until it judged."""

    def __new__(cls, value: str, spec: InputSpec, given: bool) -> "_Value":
        self = super().__new__(cls, value)
        self.spec = spec
        self.given = given
        self.absent = None
        return self


class _InputAction(argparse._StoreAction):
    """Stores the given value tagged with the declaration (``_Value``)."""

    input_spec: InputSpec

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: Optional[str] = None) -> None:
        sep = self.input_spec.separator
        # #3751: the rule of every list flag (`_lib_validation.url_list_type`):
        # an empty item is refused at parse time (rc 2), an item is not
        # stripped — a space is part of the path it names.
        if sep is not None and "" in values.split(sep):
            parser.error(f"argument {'/'.join(self.option_strings)}: an empty item in "
                         f"{values!r} ({sep!r}-separated directories)")
        setattr(namespace, self.dest, _Value(values, self.input_spec, True))


def add_input(parser: Any, flag: str, *aliases: str, select: Any = None,
              decode: Optional[Document] = None, empty: str = EMPTY_ALLOWED,
              default: Optional[str] = None, absent_default: Optional[str] = None,
              missing: str = ABSENT_REFUSED, separator: Optional[str] = None,
              **kwargs: Any) -> argparse.Action:
    """Add the input flag *flag* (and its short *aliases*, ``-i``) to
    *parser* and declare what it is: exactly one of *select* or *decode*.

    *select* — a directory input: a selector (:class:`TopLevel`,
    ``RuleSource``; see the module docstring) or ``DA_GUARD_LISTS``. *empty*:
    what an empty selection is (``EMPTY_ALLOWED`` / ``EMPTY_REFUSED``).
    *default*: the value when the flag is not given — no trailing separator
    (``rule-packs``, not ``rule-packs/``: the value is shown in messages as
    the caller would type it); *absent_default* (required with one,
    ``ABSENT_EMPTY`` / ``ABSENT_REFUSED``) is what that default is when
    nothing is there. *missing*: what a value the caller gave is when
    nothing is there (``ABSENT_REFUSED`` / ``ABSENT_EMPTY``, #3693) — only
    nothing: a regular file, a dangling link or a path it cannot reach is
    refused either way, as for a default. *separator* (#3751): the value
    is a list of directories (``a,b``), each judged on its own — an empty
    item is refused at parse time, an item is not stripped (the rule of
    ``_lib_validation.url_list_type``); ``select=DA_GUARD_LISTS`` only, no
    *default*, *missing* ``ABSENT_REFUSED``.

    *decode* — a file input whose content the tool reads: a
    :class:`Document`, read by :func:`document`. It takes no *empty*,
    *default* or *absent_default* (#3445: a file nobody gave is not read).

    *kwargs* go to ``add_argument`` (``help``, ``required``, ``metavar``);
    the value is always the string given, so ``type`` / ``nargs`` /
    ``action`` are not taken."""
    for taken in ("type", "nargs", "action", "dest"):
        if taken in kwargs:
            raise TypeError(f"add_input does not take {taken}=")
    if (select is None) == (decode is None):
        raise TypeError("add_input takes exactly one of select= (a directory) "
                        "or decode= (a file)")
    if decode is not None:
        if not isinstance(decode, Document) or decode.fmt not in FORMATS:
            raise TypeError(f"decode must be a Document with fmt in {FORMATS}, not {decode!r}")
        if (empty != EMPTY_ALLOWED or default is not None or absent_default is not None
                or missing != ABSENT_REFUSED):
            raise ValueError("a decode= input takes no empty=, default=, absent_default= "
                             "or missing=")
        if decode.empty_is is not None and decode.fmt != "yaml":
            raise ValueError("empty_is is for a YAML document only")
        tops = decode.top if isinstance(decode.top, tuple) else (decode.top,)
        if decode.empty_is is not None and decode.empty_is not in tops:
            raise ValueError(f"empty_is {decode.empty_is!r} is not a declared top {decode.top!r}")
        if (decode.fmt == "csv") != bool(decode.columns) or (
                decode.fmt == "csv" and decode.top is not list):
            raise ValueError("a csv document takes columns= (its header) and top=list; "
                             "columns= is for a csv document only")
        if any(col not in decode.columns for col, _f in decode.formats):
            raise ValueError("formats= names columns of columns= only (a csv document)")
    elif not (select == DA_GUARD_LISTS or _is_selector(select)):
        raise TypeError("select must be a selector (choose(directory, names), wanted) "
                        f"or DA_GUARD_LISTS, not {select!r}")
    if empty not in (EMPTY_ALLOWED, EMPTY_REFUSED):
        raise ValueError(f"empty must be {EMPTY_ALLOWED!r} or {EMPTY_REFUSED!r}")
    if (default is None) != (absent_default is None):
        raise ValueError("a default needs absent_default, and only a default has one")
    if absent_default not in (None, ABSENT_EMPTY, ABSENT_REFUSED):
        raise ValueError(f"absent_default must be {ABSENT_EMPTY!r} or {ABSENT_REFUSED!r}")
    if missing not in (ABSENT_EMPTY, ABSENT_REFUSED):
        raise ValueError(f"missing must be {ABSENT_EMPTY!r} or {ABSENT_REFUSED!r}")
    if separator is not None and (separator == "" or select != DA_GUARD_LISTS
                                  or default is not None or missing != ABSENT_REFUSED):
        raise ValueError("separator= is a non-empty separator of a select=DA_GUARD_LISTS "
                         "input with no default= and missing=ABSENT_REFUSED")
    if default is not None and len(default) > 1 and default.endswith(("/", os.sep)):
        raise ValueError(f"default {default!r}: no trailing separator (see add_input)")
    action = parser.add_argument(flag, *aliases, action=_InputAction, default=None, **kwargs)
    spec = InputSpec(flag, action.dest, select, empty, default, absent_default, decode,
                     missing, separator)
    action.input_spec = spec
    if default is not None:
        action.default = _Value(default, spec, False)
    return action


def _caller_error(message: str, reason: str) -> BaseException:
    """`_lib_exitcodes.CallerError`, imported only for a refusal."""
    from _lib_exitcodes import CallerError  # noqa: PLC0415  (see the module docstring)
    return CallerError(message, reason)


def _label(path: Any, flag: Optional[str]) -> str:
    shown = safe_label(str(path)) if str(path) else "''"
    return f"{flag} {shown}" if flag else shown


def refuse_empty(label: str, wanted: str, *, lang: str) -> BaseException:
    """The refusal of a selection that chose nothing (reason
    ``input_empty``), for the caller to raise: *label* names the flag and
    the value (``--rule-packs-dir rp``), *wanted* what was looked for. ⛔ The
    one wording of it (#3462)."""
    said = (f"沒有選到任何項目（{wanted}）" if lang == "zh" else f"selects nothing ({wanted})")
    return _caller_error(f"{label}: {said}", "input_empty")


def require_listed(flag: str, path: "str | os.PathLike[str]", root: Any, *,
                   lang: str) -> None:
    """Refuse a directory da-guard could not list (#3693): *root* is the
    root entry (``"."``) of da-guard's listing of *path*, the directory
    given as *flag* (a ``_lib_parse_tree.Entry``: ``unlistable_dir``,
    ``problem``), or None when the listing has none. A root it reports
    ``unlistable_dir`` raises ``dir_not_accessible`` (rc 2), named with
    da-guard's reason. ⛔ The one wording of it: a tree whose root cannot be
    listed was read as one holding no config file — config-diff's base side
    as a first import (rc 1), migrate-to-operator's source as no file (rc 0,
    #3446)."""
    if root is None or not root.unlistable_dir:
        return
    why = safe_label(root.problem[1]) if root.problem else root.role
    said = (f"無法列出目錄內容（da-guard: {why}）" if lang == "zh"
            else f"cannot list the directory (da-guard: {why})")
    raise _caller_error(f"{_label(path, flag)}: {said}", "dir_not_accessible")


def spec_of(args: argparse.Namespace, dest: str) -> InputSpec:
    """The declaration of the flag at *dest* (what ``add_input`` declared).
    Raises TypeError for a flag ``add_input`` did not declare, or one with no
    value (no default, not given)."""
    value = getattr(args, dest)
    if not isinstance(value, _Value):
        raise TypeError(f"{dest} was not declared with _lib_input.add_input, or has no value")
    return value.spec


def require_selection(args: argparse.Namespace, dest: str, chosen: "list[Any]", *,
                      wanted: str, lang: str, item: Any = None) -> "list[Any]":
    """*chosen* — what was selected from the flag declared at *dest* by a
    listing made elsewhere (``DA_GUARD_LISTS``: da-guard's) — judged by the
    declaration's ``empty``: none, under ``EMPTY_REFUSED``, is
    ``input_empty`` (*wanted*: what the listing looks for). *item*: the one
    directory of a ``separator`` list it was chosen from (named instead of
    the whole value). Returns *chosen*."""
    value = getattr(args, dest)
    if not isinstance(value, _Value):
        raise TypeError(f"{dest} was not declared with _lib_input.add_input")
    if not chosen and value.spec.empty == EMPTY_REFUSED:
        raise refuse_empty(_label(value if item is None else item, value.spec.flag), wanted,
                           lang=lang)
    return chosen


def _nothing_at(path: "str | os.PathLike[str]") -> bool:
    """True only when nothing is at *path* — not even a dangling link —
    and nothing is in its way: ``os.lstat`` answers ENOENT for it, and its
    nearest ancestor that is there is a directory. ``PathProblem.missing``
    (stat: ENOENT / ENOTDIR) is wider: a dangling link at the path or at
    an ancestor (`.da` → nowhere), and a path under a regular file, are
    "missing" to stat, yet ``ls`` shows something there (#3548)."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    else:
        return False
    for ancestor in Path(os.path.abspath(path)).parents:
        try:
            os.lstat(ancestor)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        try:  # there: it must be a directory (a link to one counts)
            return stat.S_ISDIR(os.stat(ancestor).st_mode)
        except OSError:
            return False  # a dangling link
    return True


def _list(directory: "str | os.PathLike[str]") -> "list[str]":
    """The names in *directory*, sorted. ⛔ The one listing of a directory
    in the Python tools (see the module docstring). Raises the
    ``OSError`` of a directory that cannot be listed — never ``[]``."""
    return sorted(os.listdir(directory))


def selected(args: argparse.Namespace, dest: str, *, lang: str) -> "list[Path]":
    """The paths the value of the flag declared at *dest* selects.

    A value that is not a usable directory is refused
    (``_lib_paths.classify_path``: ``dir_not_found`` /
    ``dir_not_accessible``) — given, or a default — except a default nobody
    gave with nothing there, which selects nothing under ``ABSENT_EMPTY`` —
    and a value given with nothing there under ``missing=ABSENT_EMPTY``.
    A directory that cannot be listed is ``dir_not_accessible``, given or
    default. A selector: what its ``choose`` takes of the sorted listing;
    none, under ``EMPTY_REFUSED``, is ``input_empty``. ``DA_GUARD_LISTS``:
    the directory itself (``require_selection`` judges what da-guard
    chose). A flag with no value (no default, not given) selects nothing.
    *lang* (``"en"`` / ``"zh"``) as in ``classify_path``. A selector's own
    refusal (``RuleSource``: an undeclared YAML) is raised as it is."""
    value = getattr(args, dest)
    if value is None:
        return []
    if not isinstance(value, _Value):
        raise TypeError(f"{dest} was not declared with _lib_input.add_input")
    spec = value.spec
    if spec.select is None:
        raise TypeError(f"{dest} is a file input (decode=): read it with document()")
    if not value.given:
        # argparse hands every parse the SAME default object (`action.default`):
        # judge a copy of it, so what one run found (`absent`) is never read
        # by the next parse of the same parser (#3721).
        value = _Value(str(value), spec, False)
        setattr(args, dest, value)
    if spec.separator is not None:
        # #3751: each item on its own, as one directory is judged (no
        # default, missing refused: `add_input`); all are judged before any
        # is read.
        items = value.split(spec.separator)
        raise_path_problems([classify_path(i, "dir", lang=lang, flag=spec.flag) for i in items])
        value.absent = False
        return [Path(i) for i in items]  # DA_GUARD_LISTS only: `declared_trees` judges
    problem = classify_path(value, "dir", lang=lang, flag=spec.flag)
    if problem is not None:
        nothing = (spec.missing if value.given else spec.absent_default) == ABSENT_EMPTY
        if not (nothing and _nothing_at(value)):
            raise _caller_error(problem.message, problem.reason)
        value.absent = True
        judged(spec.flag, value)
        return []
    value.absent = False
    if spec.select == DA_GUARD_LISTS:
        # Not judged yet: what da-guard lists there is (`declared_trees`).
        return [Path(value)]
    try:
        names = _list(value)
    except OSError as exc:
        missing = exc.errno in _STAT_MISSING
        said = _DETAIL[lang]["missing" if missing else "inaccessible"](exc)
        raise _caller_error(f"{_label(value, spec.flag)}: {said}",
                            "dir_not_found" if missing else "dir_not_accessible") from exc
    chosen = spec.select.choose(Path(value), names)
    if not chosen and spec.empty == EMPTY_REFUSED:
        raise refuse_empty(_label(value, spec.flag), spec.select.wanted, lang=lang)
    judged(spec.flag, value)
    return chosen


def absent(args: argparse.Namespace, dest: str) -> bool:
    """Whether ``selected`` found nothing at the path of the flag declared at
    *dest* (#3721) — read, never judged again (see the module docstring). A
    flag with no value (no default, not given) is absent. Raises TypeError
    before ``selected`` judged it: there is no answer yet to read."""
    value = getattr(args, dest)
    if value is None:
        return True
    if not isinstance(value, _Value):
        raise TypeError(f"{dest} was not declared with _lib_input.add_input")
    if value.absent is None:
        raise TypeError(f"{dest}: absent() reads what selected() judged — call selected() first")
    return value.absent


def read_bytes(path: "str | os.PathLike[str]", *, flag: Optional[str], lang: str) -> bytes:
    """The bytes of the file *path*, selected through *flag*. A file that
    cannot be read — gone since it was listed, a directory, a mode that
    denies it — is refused, named: reason ``file_unreadable`` (#3445)."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        missing = exc.errno in _STAT_MISSING
        said = _DETAIL[lang]["missing" if missing else "inaccessible"](exc)
        raise _caller_error(f"{_label(path, flag)}: {said}", "file_unreadable") from exc


def kind_of(data: Any) -> Any:
    """The top-level kind of a parsed *data*, as ``refuse_top`` takes it."""
    return type(data)


def refuse_top(label: str, got: Any, wanted: Any, *, lang: str) -> BaseException:
    """The refusal of a file whose top level is not of the type its reader
    needs (reason ``file_unreadable``), for the caller to raise: *label*
    names the flag and the path (``--mapping m.yaml``), *got* is the kind
    found (a type, ``kind_of(data)``, or ``"scalar"``), *wanted* the type
    or tuple of types needed. ⛔ The one wording of it (#3445): ``document``
    says it, and a reader that is not ``document`` (a file da-guard reads)
    says it here."""
    if lang == "zh":
        said = f"頂層不是{_wanted_words(wanted, lang)}（實為{_noun(got, lang)}）"
    else:
        said = f"the top level is not {_wanted_words(wanted, lang)} (got {_noun(got, lang)})"
    return _caller_error(f"{label}: {said}", "file_unreadable")


def document(args: argparse.Namespace, dest: str, *, lang: str) -> Any:
    """The parsed content of the file the flag declared at *dest* (with
    ``decode=``) names; None when the flag was not given.

    The path is judged first (``_lib_paths.classify_path``, ``"file"``:
    ``file_not_found`` / ``file_not_accessible``), then read
    (``read_bytes``), decoded (``decode_bytes``) and its top level checked
    against the declaration — each failure a :class:`_lib_exitcodes.
    CallerError` (rc 2) whose message is ``<flag> <path>: <detail>``,
    reason ``file_unreadable`` for the content. A YAML empty document is
    ``empty_is()`` when the declaration says so, else of the wrong type.
    *lang* (``"en"`` / ``"zh"``) as in ``classify_path``."""
    value = getattr(args, dest)
    if value is None:
        return None
    if not isinstance(value, _Value):
        raise TypeError(f"{dest} was not declared with _lib_input.add_input")
    spec = value.spec
    if spec.decode is None:
        raise TypeError(f"{dest} is a directory input (select=): read it with selected()")
    doc = spec.decode
    problem = classify_path(value, "file", lang=lang, flag=spec.flag)
    if problem is not None:
        raise _caller_error(problem.message, problem.reason)
    raw = read_bytes(value, flag=spec.flag, lang=lang)
    label = _label(value, spec.flag)
    try:
        data = decode_bytes(raw, fmt=doc.fmt, name=str(value))
    except DocumentError as exc:
        raise _caller_error(f"{label}: {safe_label(exc.detail(lang))}",
                            "file_unreadable") from exc
    if doc.fmt == "csv":
        records = _csv_records(data, doc.columns, doc.formats, label, lang)
        judged(spec.flag, value)
        return records
    if data is None and doc.empty_is is not None:
        data = doc.empty_is()  # judged by the top-level check like any value
    if not isinstance(data, doc.top):
        raise refuse_top(label, kind_of(data), doc.top, lang=lang)
    judged(spec.flag, value)
    return data


def _csv_records(rows: "list[list[str]]", columns: tuple, formats: tuple, label: str,
                 lang: str) -> "list[dict[str, str]]":
    """The data rows of a csv document as dicts, judged against its
    declared *columns* (#3662): the header must be exactly *columns*, every
    row must have as many fields, and at least one data row must be there.
    Each refusal is reason ``file_unreadable``, naming *label*."""
    zh = lang == "zh"
    rows = [r for r in rows if r]  # a blank line is no row (as csv.DictReader reads it)
    wanted = ",".join(columns)
    header = rows[0] if rows else None
    if header is None or tuple(header) != tuple(columns):
        got = ",".join(header) if header else ("（無）" if zh else "(none)")
        said = (f"表頭不是 {wanted}（實為 {got}）" if zh
                else f"the header is not {wanted} (got {got})")
        raise _caller_error(f"{label}: {safe_label(said)}", "file_unreadable")
    for n, row in enumerate(rows[1:], start=1):
        if len(row) != len(columns):
            said = (f"第 {n} 筆資料列有 {len(row)} 個欄位，表頭有 {len(columns)} 個" if zh
                    else f"data row {n} has {len(row)} fields, the header {len(columns)}")
            raise _caller_error(f"{label}: {said}", "file_unreadable")
    if len(rows) == 1:
        said = "只有表頭，沒有資料列" if zh else "a header and no data row"
        raise _caller_error(f"{label}: {said}", "file_unreadable")
    records = [dict(zip(columns, row)) for row in rows[1:]]
    for n, record in enumerate(records, start=1):
        for col, form in formats:
            try:
                datetime.strptime(record[col].strip(), form)
            except ValueError:
                said = (f"第 {n} 筆資料列的 {col} {record[col]!r} 不是 {form}" if zh
                        else f"data row {n}: {col} {record[col]!r} is not {form}")
                raise _caller_error(f"{label}: {safe_label(said)}", "file_unreadable") from None
    return records
