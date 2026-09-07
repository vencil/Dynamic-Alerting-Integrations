"""Write-failure class gate for the secure writers (#1641).

THE DEFECT CLASS
----------------
``_lib_io.write_text_secure`` / ``write_json_secure`` used to let the
``OSError`` family escape. A mistyped ``-o`` therefore ended in a traceback at
rc=1 — and rc=1 in this repo is ``EXIT_VIOLATION`` ("the tool ran, your
config has a finding"), while the exit-code SSOT (``_lib_exitcodes``) puts
"IO failure / unexpected crash" under ``EXIT_CALLER_ERROR`` (2). Measured on
``origin/main`` with the same AST scanner this file carries: 46 call sites,
4 guarded, 42 unguarded, across 21 modules.

THE FIX SHAPE (and what this file pins)
---------------------------------------
The class is closed at the helper, not at 42 call sites:

* the secure writers raise ``OutputWriteError`` (an ``OSError`` subclass, so
  the 4 pre-existing ``except OSError`` guards keep working);
* ``write_text_or_die`` / ``write_json_or_die`` / ``ensure_dir_or_die`` turn
  it into ``ERROR: <one line>`` + rc=2, naming the flag the path came from;
* tool modules call the ``_or_die`` sisters; shared-library modules keep the
  raising form and the TOOL that calls them catches ``OutputWriteError``
  once in ``main()``.

Two things are asserted, both DERIVED from the tree rather than enumerated:

1. **Tripwire** — every raw ``write_text_secure(`` / ``write_json_secure(``
   call under ``scripts/tools/`` (outside ``_lib_io.py`` itself) is either
   inside a ``try`` whose handlers can catch ``OSError`` /
   ``OutputWriteError``, or lives in one of the shared-library modules in
   ``RAW_CALL_LIBRARY_MODULES``. That allowlist is exit-locked: every entry
   must still exist AND still hold ≥1 raw call, so it can only shrink.
2. **Scanner controls** — synthetic snippets prove the scanner sees what it
   claims to see (bare call ⇒ UNGUARDED, ``except OSError`` ⇒ GUARDED,
   ``except ValueError`` ⇒ UNGUARDED, call inside a nested ``def`` under a
   ``try`` ⇒ UNGUARDED). Without these a scanner that classifies everything
   GUARDED would pass the tripwire vacuously.

The unit tests below pin the helper contract itself (message shape, errno,
``from`` chaining, rc, no traceback, 0o600 kept on success, ``TypeError``
NOT converted). The end-to-end rows (real tool, bad ``-o``, rc=2 + control)
live in ``tests/shared/test_output_path_write_failure.py``.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import _lib_io  # noqa: E402  (sys.path via tests/conftest.py)
import _lib_python  # noqa: E402
import _lib_yaml  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"

WRITER_NAMES = frozenset({"write_text_secure", "write_json_secure"})

# Handler names that CAN catch what the secure writers raise. ``IOError`` is an
# alias of ``OSError``; ``Exception`` / ``BaseException`` / a bare ``except``
# catch it by inclusion. Narrower OSError subclasses (``FileNotFoundError``)
# do NOT catch an ``OutputWriteError`` and are deliberately absent.
CATCHING_HANDLER_NAMES = frozenset({
    "OSError", "IOError", "OutputWriteError", "Exception", "BaseException",
})

# Shared-library modules that keep the RAISING form on purpose: several tools
# reach them, and the tool that owns the CLI path catches ``OutputWriteError``
# once in ``main()``. Repo-relative POSIX paths. ⛔ Exit-locked — see
# ``test_library_allowlist_cannot_go_stale``: an entry that stops existing or
# stops holding a raw call must be REMOVED here, so the list only shrinks.
RAW_CALL_LIBRARY_MODULES = frozenset({
    "scripts/tools/_lib_yaml.py",
    "scripts/tools/ops/_registry_lib.py",
    "scripts/tools/ops/_observed_map_lib.py",
})


# ═══════════════════════════════════════════════════════════════════════════
# Scanner
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class WriterCall:
    file: str          # repo-relative POSIX path (or "<snippet>")
    line: int
    name: str
    guarded: bool

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _handler_can_catch(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True  # bare except
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for t in types:
        if isinstance(t, ast.Name) and t.id in CATCHING_HANDLER_NAMES:
            return True
        if isinstance(t, ast.Attribute) and t.attr in CATCHING_HANDLER_NAMES:
            return True
    return False


def scan_source(source: str, label: str = "<snippet>") -> list[WriterCall]:
    """Every ``write_text_secure(`` / ``write_json_secure(`` call in *source*.

    A call is GUARDED iff, walking up from the call WITHOUT crossing a
    function / class / lambda boundary, some ancestor ``try`` has the call in
    its ``body`` (not ``handlers`` / ``orelse`` / ``finalbody``) and at least
    one handler that can catch ``OSError`` or ``OutputWriteError``.
    """
    tree = ast.parse(source, label)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found: list[WriterCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name not in WRITER_NAMES:
            continue
        guarded = False
        cur: ast.AST = node
        while cur in parents:
            parent = parents[cur]
            if isinstance(parent, _SCOPE_BOUNDARIES):
                break
            if isinstance(parent, ast.Try) and cur in parent.body:
                if any(_handler_can_catch(h) for h in parent.handlers):
                    guarded = True
                    break
            cur = parent
        found.append(WriterCall(label, node.lineno, name, guarded))
    return found


def scan_tree() -> list[WriterCall]:
    calls: list[WriterCall] = []
    for py in sorted(TOOLS_DIR.rglob("*.py")):
        if py.name == "_lib_io.py":
            continue  # the helper itself
        rel = py.relative_to(REPO_ROOT).as_posix()
        calls.extend(scan_source(py.read_text(encoding="utf-8"), rel))
    return calls


# ═══════════════════════════════════════════════════════════════════════════
# Scanner controls — the instrument must see guards AND their absence
# ═══════════════════════════════════════════════════════════════════════════
class TestScannerControls:
    def test_bare_call_is_unguarded(self):
        calls = scan_source("write_text_secure(p, c)\n")
        assert [c.guarded for c in calls] == [False]

    def test_except_oserror_is_guarded(self):
        src = "try:\n    write_text_secure(p, c)\nexcept OSError:\n    pass\n"
        assert [c.guarded for c in scan_source(src)] == [True]

    def test_except_outputwriteerror_is_guarded(self):
        src = ("try:\n    write_json_secure(p, d)\n"
               "except OutputWriteError as exc:\n    pass\n")
        assert [c.guarded for c in scan_source(src)] == [True]

    def test_except_valueerror_is_unguarded(self):
        src = "try:\n    write_text_secure(p, c)\nexcept ValueError:\n    pass\n"
        assert [c.guarded for c in scan_source(src)] == [False]

    def test_narrow_oserror_subclass_is_unguarded(self):
        """``FileNotFoundError`` cannot catch an ``OutputWriteError``."""
        src = ("try:\n    write_text_secure(p, c)\n"
               "except FileNotFoundError:\n    pass\n")
        assert [c.guarded for c in scan_source(src)] == [False]

    def test_tuple_handler_containing_oserror_is_guarded(self):
        src = ("try:\n    write_text_secure(p, c)\n"
               "except (ValueError, OSError):\n    pass\n")
        assert [c.guarded for c in scan_source(src)] == [True]

    def test_call_in_nested_def_under_try_is_unguarded(self):
        """A ``try`` around a ``def`` does not guard the body at call time."""
        src = ("try:\n    def f():\n        write_text_secure(p, c)\n"
               "except OSError:\n    pass\n")
        assert [c.guarded for c in scan_source(src)] == [False]

    def test_call_in_handler_body_is_unguarded(self):
        src = ("try:\n    pass\nexcept OSError:\n    write_text_secure(p, c)\n")
        assert [c.guarded for c in scan_source(src)] == [False]

    def test_attribute_form_is_seen(self):
        calls = scan_source("lib.write_json_secure(p, d)\n")
        assert [(c.name, c.guarded) for c in calls] == [("write_json_secure", False)]

    def test_sister_helpers_are_not_counted(self):
        """``write_text_or_die`` is the FIX; it must not read as a raw call."""
        assert scan_source("write_text_or_die(p, c, flag='-o')\n") == []


# ═══════════════════════════════════════════════════════════════════════════
# Tripwire — derived from the tree
# ═══════════════════════════════════════════════════════════════════════════
def test_population_is_not_vacuous():
    calls = scan_tree()
    assert len(calls) >= 4, (
        "fewer than 4 secure-writer calls under scripts/tools — the scan root "
        "or the writer names have drifted; this gate would pass vacuously")


def test_library_allowlist_cannot_go_stale():
    """Exit-lock: every allowlisted module exists and still holds a raw call."""
    by_file: dict[str, list[WriterCall]] = {}
    for c in scan_tree():
        by_file.setdefault(c.file, []).append(c)
    stale = []
    for rel in sorted(RAW_CALL_LIBRARY_MODULES):
        if not (REPO_ROOT / rel).is_file():
            stale.append(f"{rel}: file no longer exists")
        elif not any(not c.guarded for c in by_file.get(rel, [])):
            stale.append(f"{rel}: no raw secure-writer call left — remove it "
                         "from RAW_CALL_LIBRARY_MODULES")
    assert not stale, "\n".join(stale)
    # And the allowlist really is library modules, not tools in disguise.
    for rel in RAW_CALL_LIBRARY_MODULES:
        assert Path(rel).name.startswith("_"), (
            f"{rel} is not a _-prefixed library module; a tool must use the "
            "_or_die sisters instead of being allowlisted")


def test_no_tool_module_calls_the_raw_writer_unguarded():
    """Every raw ``write_*_secure(`` under scripts/tools is guarded or in a lib.

    An UNGUARDED call in a tool module is the #1641 defect re-opened: a
    mistyped output path there ends in a traceback at rc=1 again. Fix by
    switching the site to ``write_text_or_die`` / ``write_json_or_die`` (with
    ``flag=`` when the path comes from argv), or — for a shared library — by
    adding the module to ``RAW_CALL_LIBRARY_MODULES`` and catching
    ``OutputWriteError`` in every tool that calls it.
    """
    offenders = [
        f"  {c.where}  {c.name}(...)"
        for c in scan_tree()
        if not c.guarded and c.file not in RAW_CALL_LIBRARY_MODULES
    ]
    assert not offenders, (
        f"{len(offenders)} unguarded secure-writer call(s) in tool modules "
        "(#1641 write-failure class):\n" + "\n".join(offenders))


# ═══════════════════════════════════════════════════════════════════════════
# Helper contract — OutputWriteError and the _or_die sisters
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def missing_parent(tmp_path: Path) -> Path:
    """A path whose parent directory does not exist (ENOENT on open)."""
    return tmp_path / "absent" / "out.txt"


@pytest.fixture
def under_a_file(tmp_path: Path) -> Path:
    """A path whose parent is a regular FILE (ENOTDIR on open / mkdir).

    Root-safe: permission bits are ignored by uid 0, but a file in the way
    of a directory component fails for everyone.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    return blocker / "out.txt"


class TestOutputWriteError:
    def test_is_an_oserror_and_keeps_errno_strerror_filename(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_text_secure(str(missing_parent), "x", flag="-o/--output")
        exc = ei.value
        assert isinstance(exc, OSError)
        assert exc.errno == 2
        assert exc.strerror
        assert exc.filename == str(missing_parent)
        assert exc.path == str(missing_parent)
        assert exc.flag == "-o/--output"
        assert isinstance(exc.cause, FileNotFoundError)
        assert exc.__cause__ is exc.cause  # raised `from exc`

    def test_message_names_path_errno_and_flag(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_text_secure(str(missing_parent), "x", flag="-o/--output")
        msg = str(ei.value)
        assert msg.startswith(f"cannot write {missing_parent}: ")
        assert "(errno 2)" in msg
        assert msg.endswith("— check the value given to -o/--output")
        assert "internal output path" not in msg

    def test_message_without_flag_says_internal_path(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_json_secure(str(missing_parent), {"a": 1})
        msg = str(ei.value)
        assert msg.startswith(f"cannot write {missing_parent}: ")
        assert msg.endswith(
            "— internal output path, this is a bug or an unwritable workspace")
        assert "check the value given to" not in msg

    def test_existing_except_oserror_guards_still_catch_it(self, missing_parent):
        """The 4 pre-existing ``except OSError`` sites keep working unchanged."""
        caught = None
        try:
            _lib_io.write_text_secure(str(missing_parent), "x")
        except OSError as exc:
            caught = exc
        assert isinstance(caught, _lib_io.OutputWriteError)

    def test_parent_is_a_file_is_the_same_class(self, under_a_file):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_text_secure(str(under_a_file), "x", flag="--out")
        assert ei.value.errno is not None
        assert "check the value given to --out" in str(ei.value)

    def test_json_type_error_is_not_converted(self, tmp_path):
        """Non-serialisable data is a programming error, not a path problem."""
        target = tmp_path / "x.json"
        with pytest.raises(TypeError):
            _lib_io.write_json_secure(str(target), {"f": object()})

    def test_success_path_unchanged_lf_and_0o600(self, tmp_path):
        target = tmp_path / "ok.txt"
        _lib_io.write_text_secure(str(target), "a\nb\n", flag="-o")
        assert target.read_bytes() == b"a\nb\n"
        if os.name != "nt":
            assert target.stat().st_mode & 0o777 == 0o600
        jt = tmp_path / "ok.json"
        _lib_io.write_json_secure(str(jt), {"k": "v"}, flag="-o")
        assert json.loads(jt.read_text(encoding="utf-8")) == {"k": "v"}


class TestOrDieSisters:
    def test_write_text_or_die_exits_2_with_one_line(self, missing_parent, capsys):
        with pytest.raises(SystemExit) as ei:
            _lib_io.write_text_or_die(str(missing_parent), "x", flag="-o/--output")
        assert ei.value.code == EXIT_CALLER_ERROR == 2
        out, err = capsys.readouterr()
        assert out == ""
        lines = err.strip().splitlines()
        assert len(lines) == 1, err
        assert lines[0].startswith("ERROR: cannot write ")
        assert str(missing_parent) in lines[0]
        assert "-o/--output" in lines[0]
        assert "Traceback" not in err

    def test_write_json_or_die_exits_2(self, under_a_file, capsys):
        with pytest.raises(SystemExit) as ei:
            _lib_io.write_json_or_die(str(under_a_file), {"a": 1}, flag="--output")
        assert ei.value.code == 2
        _, err = capsys.readouterr()
        assert err.startswith("ERROR: cannot write ")
        assert "check the value given to --output" in err

    def test_exit_code_is_overridable(self, missing_parent):
        with pytest.raises(SystemExit) as ei:
            _lib_io.write_text_or_die(str(missing_parent), "x", exit_code=7)
        assert ei.value.code == 7

    def test_or_die_writes_normally_on_a_good_path(self, tmp_path):
        target = tmp_path / "good.txt"
        _lib_io.write_text_or_die(str(target), "hello", flag="-o")
        assert target.read_text(encoding="utf-8") == "hello"
        jt = tmp_path / "good.json"
        _lib_io.write_json_or_die(str(jt), [1, 2], flag="-o")
        assert json.loads(jt.read_text(encoding="utf-8")) == [1, 2]

    def test_ensure_dir_raises_the_same_class_on_a_file_component(self, under_a_file):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.ensure_dir(under_a_file / "deeper", flag="-o/--output-dir")
        msg = str(ei.value)
        assert msg.startswith(f"cannot create directory {under_a_file / 'deeper'}: ")
        assert msg.endswith("— check the value given to -o/--output-dir")
        assert ei.value.action == "create directory"

    def test_ensure_dir_or_die_exits_2(self, under_a_file, capsys):
        with pytest.raises(SystemExit) as ei:
            _lib_io.ensure_dir_or_die(under_a_file, flag="--out")
        assert ei.value.code == 2
        _, err = capsys.readouterr()
        assert err.startswith("ERROR: cannot create directory ")
        assert "Traceback" not in err

    def test_ensure_dir_creates_parents_and_is_idempotent(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        _lib_io.ensure_dir(target)
        _lib_io.ensure_dir_or_die(target)
        assert target.is_dir()


class TestLibraryPassthrough:
    """Library wrappers forward ``flag`` so the tool-level handler's message
    still names the operator's flag."""

    def test_write_yaml_crd_forwards_flag(self, missing_parent):
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_yaml.write_yaml_crd(missing_parent, {"a": 1}, flag="--output-dir")
        assert ei.value.flag == "--output-dir"
        assert "check the value given to --output-dir" in str(ei.value)

    def test_write_onboard_hints_forwards_flag(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("file\n", encoding="utf-8")
        with pytest.raises(_lib_io.OutputWriteError) as ei:
            _lib_io.write_onboard_hints(str(blocker), {"tenants": ["alpha"]},
                                        flag="-o/--output-dir")
        assert ei.value.flag == "-o/--output-dir"

    def test_facade_re_exports_the_new_names(self):
        for name in ("OutputWriteError", "write_text_or_die", "write_json_or_die",
                     "ensure_dir", "ensure_dir_or_die"):
            assert getattr(_lib_python, name) is getattr(_lib_io, name), name


def test_helper_module_has_no_import_cycle_with_exitcodes():
    """``_lib_io`` imports ``EXIT_CALLER_ERROR`` from the SSOT directly; this
    only works while ``_lib_exitcodes`` does not import ``_lib_io`` back."""
    src = (TOOLS_DIR / "_lib_exitcodes.py").read_text(encoding="utf-8")
    assert "_lib_io" not in src
    assert _lib_io.EXIT_CALLER_ERROR == 2
    assert sys.modules["_lib_io"].write_text_or_die.__defaults__ is None
