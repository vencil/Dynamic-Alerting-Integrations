"""Which files a value the caller gave selects, and the bytes read from them (#3446).

The one answer to "the caller gave this value — which files does the run
end up with, and what does it read from them". Standard library only, and
`_lib_exitcodes` (whose import installs the crash hook) is imported only
when a refusal is raised, as in `_lib_paths`.

* ``add_input(parser, flag, *, select, empty, default, absent_default)`` —
  declares an input flag. ⛔ The declaration is on the argparse action
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
* ``require_selection(args, dest, chosen, *, wanted, lang)`` — the same
  ``empty`` judgement for a selection made elsewhere (``DA_GUARD_LISTS``:
  what da-guard's listing chose), and ``refuse_empty(label, wanted, *,
  lang)`` for a selection no ``add_input`` flag makes (#3462). ⛔ The one
  wording of "selected nothing".
* ``read_bytes(path, *, flag, lang)`` — the bytes of a selected file; a
  file that cannot be read is reason ``file_unreadable``.

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
the tool refuses a listing whose root da-guard reports ``unlistable_dir``.

⛔ ``select`` (#3462) is a selector: :class:`TopLevel`, or any declaration of
which top-level names are the input — ``_lib_consumer_read.RuleSource``
(``RULE_PACKS``) is one — that answers ``choose(directory, names)`` and
``wanted``. ``selected`` lists the directory (once, ``_list``) and hands the
names over: the selector judges names, it never lists.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import stat
from pathlib import Path
from typing import Any, NamedTuple, Optional

from _lib_label import safe_label
from _lib_paths import _DETAIL, _STAT_MISSING, classify_path

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


class InputSpec(NamedTuple):
    """What ``add_input`` declared for one flag."""
    flag: str
    dest: str
    select: Any
    """A selector (:class:`TopLevel`, ``RuleSource``) or ``DA_GUARD_LISTS``."""
    empty: str
    """``EMPTY_ALLOWED`` / ``EMPTY_REFUSED``."""
    default: Optional[str]
    absent_default: Optional[str]
    """``ABSENT_EMPTY`` / ``ABSENT_REFUSED``; None when there is no default."""


class _Value(str):
    """The flag's value as the namespace holds it: the string, plus the
    declaration and whether the caller gave it (a default nobody gave is
    read by ``absent_default``)."""

    spec: InputSpec
    given: bool

    def __new__(cls, value: str, spec: InputSpec, given: bool) -> "_Value":
        self = super().__new__(cls, value)
        self.spec = spec
        self.given = given
        return self


class _InputAction(argparse._StoreAction):
    """Stores the given value tagged with the declaration (``_Value``)."""

    input_spec: InputSpec

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: Optional[str] = None) -> None:
        setattr(namespace, self.dest, _Value(values, self.input_spec, True))


def add_input(parser: Any, flag: str, *, select: Any, empty: str = EMPTY_ALLOWED,
              default: Optional[str] = None, absent_default: Optional[str] = None,
              **kwargs: Any) -> argparse.Action:
    """Add the input flag *flag* to *parser* and declare what it selects.

    *select*: a selector (:class:`TopLevel`, ``RuleSource``; see the module
    docstring) or ``DA_GUARD_LISTS``. *empty*: what an empty selection is
    (``EMPTY_ALLOWED`` / ``EMPTY_REFUSED``). *default*: the value when the
    flag is not given — no trailing separator (``rule-packs``, not
    ``rule-packs/``: the value is shown in messages as the caller would
    type it); *absent_default* (required with one,
    ``ABSENT_EMPTY`` / ``ABSENT_REFUSED``) is what that default is when
    nothing is there. *kwargs* go to ``add_argument``
    (``help``, ``required``, ``metavar``); the value is always the string
    given, so ``type`` / ``nargs`` / ``action`` are not taken."""
    for taken in ("type", "nargs", "action", "dest"):
        if taken in kwargs:
            raise TypeError(f"add_input does not take {taken}=")
    if not (select == DA_GUARD_LISTS or _is_selector(select)):
        raise TypeError("select must be a selector (choose(directory, names), wanted) "
                        f"or DA_GUARD_LISTS, not {select!r}")
    if empty not in (EMPTY_ALLOWED, EMPTY_REFUSED):
        raise ValueError(f"empty must be {EMPTY_ALLOWED!r} or {EMPTY_REFUSED!r}")
    if (default is None) != (absent_default is None):
        raise ValueError("a default needs absent_default, and only a default has one")
    if absent_default not in (None, ABSENT_EMPTY, ABSENT_REFUSED):
        raise ValueError(f"absent_default must be {ABSENT_EMPTY!r} or {ABSENT_REFUSED!r}")
    if default is not None and len(default) > 1 and default.endswith(("/", os.sep)):
        raise ValueError(f"default {default!r}: no trailing separator (see add_input)")
    action = parser.add_argument(flag, action=_InputAction, default=None, **kwargs)
    spec = InputSpec(flag, action.dest, select, empty, default, absent_default)
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


def require_selection(args: argparse.Namespace, dest: str, chosen: "list[Any]", *,
                      wanted: str, lang: str) -> "list[Any]":
    """*chosen* — what was selected from the flag declared at *dest* by a
    listing made elsewhere (``DA_GUARD_LISTS``: da-guard's) — judged by the
    declaration's ``empty``: none, under ``EMPTY_REFUSED``, is
    ``input_empty`` (*wanted*: what the listing looks for). Returns
    *chosen*."""
    value = getattr(args, dest)
    if not isinstance(value, _Value):
        raise TypeError(f"{dest} was not declared with _lib_input.add_input")
    if not chosen and value.spec.empty == EMPTY_REFUSED:
        raise refuse_empty(_label(value, value.spec.flag), wanted, lang=lang)
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
    gave with nothing there, which selects nothing under ``ABSENT_EMPTY``.
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
    problem = classify_path(value, "dir", lang=lang, flag=spec.flag)
    if problem is not None:
        if value.given or spec.absent_default == ABSENT_REFUSED or not _nothing_at(value):
            raise _caller_error(problem.message, problem.reason)
        return []
    if spec.select == DA_GUARD_LISTS:
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
    return chosen


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
