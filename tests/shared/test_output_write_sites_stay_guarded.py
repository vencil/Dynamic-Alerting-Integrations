"""Static pin: a RAW output sink in a #1789 file stays guarded (Phase A skeleton).

WHAT THIS FILE IS FOR, AND WHAT IT IS NOT
-----------------------------------------
``test_write_failure_class`` pins the SECURE-WRITER population: every
``write_text_secure`` / ``write_json_secure`` call is guarded or allowlisted.
That population is closed at the helper, so it stays closed on its own.

The raw sinks are the other half and they have no helper to close them. A
tool that writes with a bare ``open(..., "w")``, ``Path.mkdir``,
``os.makedirs``, ``shutil.copy2`` or ``os.replace`` cannot move to the secure
writers without changing the bytes or the permissions it emits, so #1789
WRAPS those sites in ``_lib_io.output_write`` instead. A wrapper is not a
helper: nothing stops the next edit from adding a raw sink beside a wrapped
one, or from deleting the ``with`` line. That is what this file watches.

⛔ **The measured wording, stated so nobody over-reads a green run.** The
``write_*_secure`` population has a static guard over the WHOLE tree. The raw
sink population has a pin only for the files this ticket touched — the ones
listed in ``GUARDED_FILES``. Everything else that writes raw is unpinned, and
this file does not know about it. Do not read this as "raw sinks are
guarded"; read it as "the raw sinks in these named files are".

WHAT COUNTS AS GUARDED
----------------------
A sink is guarded when, walking up from it WITHOUT crossing a ``def`` /
``class`` / ``lambda`` boundary, one of these holds:

1. it is in the body of a ``with output_write(...)`` (#1789's wrapper);
2. the call is itself one of the already-closed writers — any name ending in
   ``_or_die`` or ``_secure``. Those are judged by ``test_write_failure_class``,
   not here; recording them keeps a CONVERTED site visible in this file's
   inventory instead of silently vanishing from it;
3. it is in the body of a ``try`` whose handler can catch ``OSError`` /
   ``OutputWriteError`` **and actually stops** — the handler body raises,
   calls ``sys.exit``, or returns something other than ``EXIT_OK``.

Arm 3 is deliberately STRICTER than the one in ``test_write_failure_class``.
``ops/da_assembler`` measured rc=0 on an unwritable ``--config-dir`` because
its ``except Exception:`` logged and carried on: the write-failure class was
"caught" and the operator got a success. A handler that swallows is not a
guard, it is the other failure mode of the same defect.

THE SCANNER
-----------
Reuses ``test_write_failure_class``: the same handler predicate
(``_handler_can_catch`` over ``CATCHING_HANDLER_NAMES``) and the same scope
boundaries (``_SCOPE_BOUNDARIES``), imported rather than restated, plus a
control that its walk-up rule and this one agree on the shapes they share.

⚠️ **Measured blind spot, on purpose.** ``Path.replace`` / ``Path.rename``
(method form) are NOT sinks here, while ``os.replace`` / ``os.rename`` are.
Reason: 174 ``.replace(`` call sites under ``scripts/tools/`` and all but the
``os.``-qualified ones are ``str.replace``. A predicate with that false-red
rate does not survive contact with the next person to hit it (rulebook D-05e:
measure the false-red surface BEFORE choosing the predicate). One real
method-form site exists — ``dx/migrate_ssot_language.py:357``
``source.rename(target)`` — and it is not an argv output path. A file that
lands in ``GUARDED_FILES`` with an atomic-rename site must spell it
``os.replace``.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

# The scanner shape is REUSED, not restated: same handler predicate, same
# scope boundaries, same "walk up, stop at a def" rule.
import test_write_failure_class as _wfc  # noqa: E402  (tests/shared on sys.path)
from test_write_failure_class import (  # noqa: E402
    CATCHING_HANDLER_NAMES,
    _handler_can_catch,
    _SCOPE_BOUNDARIES,
    scan_source as _secure_writer_scan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "scripts" / "tools"

# ── The sink vocabulary ────────────────────────────────────────────────────
# Module-qualified: only counted as `<module>.<attr>(...)`, because the bare
# attribute name is ambiguous (`dict.copy`, `str.replace` — measured above).
MODULE_SINKS: dict[str, frozenset[str]] = {
    "os": frozenset({"makedirs", "mkdir", "replace", "rename", "fdopen", "chmod"}),
    "shutil": frozenset({"copy", "copy2", "copyfile", "copytree", "copymode",
                         "copystat", "move"}),
    "tempfile": frozenset({"mkstemp"}),
}
# Unambiguous write methods of a path-like object: no builtin type has these
# with a non-writing meaning, so any receiver counts.
PATH_METHOD_SINKS = frozenset({"write_text", "write_bytes", "mkdir", "chmod", "touch"})
# `open` and its aliases; only a WRITE mode is a sink.
OPEN_NAMES = frozenset({"open"})
OPEN_MODULE_NAMES = {"io": frozenset({"open"}), "codecs": frozenset({"open"})}
WRITE_MODE_CHARS = frozenset("wax+")

WRAPPER_NAME = "output_write"
GUARD_KINDS = ("output_write", "or_die", "try")

# Handler bodies that end the run rather than swallow. `EXIT_OK` / `0` /
# a bare `return` are the swallow shapes (ops/da_assembler measured rc=0).
_OK_RETURNS = frozenset({"EXIT_OK"})
_EXITING_CALLS = frozenset({"exit", "_exit", "_die_on_write_error", "error", "fail"})

# ── The population ─────────────────────────────────────────────────────────
# ⛔ Repo-relative POSIX paths of the tool files this ticket wraps. EMPTY in
# Phase A: the helpers and this scanner land before the sites do, so there is
# nothing to pin yet and `test_no_raw_sink_in_a_guarded_file_is_bare` is
# vacuously true. The ceiling below says so out loud instead of letting a
# green run be mistaken for coverage. Phase B adds files here AND raises the
# ceiling in the same diff.
GUARDED_FILES: tuple[str, ...] = ()
_GUARDED_FILES_CEILING = 0

# Sites inside a GUARDED_FILES file that stay unguarded on purpose, as
# `"<repo-relative path>:<line>"` → reason. Exit-locked the same way: an
# entry that no longer names an unguarded sink must be REMOVED, so the list
# only shrinks. EMPTY in Phase A for the same reason as above.
NOT_GUARDED: dict[str, str] = {}
_NOT_GUARDED_CEILING = 0


# ═══════════════════════════════════════════════════════════════════════════
# Scanner
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SinkCall:
    file: str          # repo-relative POSIX path (or "<snippet>")
    line: int
    sink: str          # how it is spelled, e.g. "open('w')", "os.makedirs"
    guard: str | None  # one of GUARD_KINDS, or None

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def guarded(self) -> bool:
        return self.guard is not None


def _name_of(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _open_mode_is_write(node: ast.Call) -> bool | None:
    """True / False / None ("cannot tell") for an ``open``-family call.

    A missing mode is text-read, so not a sink. A mode that is not a literal
    (a variable, an f-string) is UNKNOWABLE here and counts as a write: this
    gate should fail loudly on a site it cannot classify rather than pass it
    (rulebook D-05g — the worst failure of a classifier is not judging).
    """
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(set(mode.value) & WRITE_MODE_CHARS)
    return None


def _classify_sink(node: ast.Call) -> str | None:
    """The sink label for *node*, or ``None`` if it does not write."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        mod, attr = func.value.id, func.attr
        if attr in MODULE_SINKS.get(mod, ()):
            return f"{mod}.{attr}"
        if attr in OPEN_MODULE_NAMES.get(mod, ()):
            verdict = _open_mode_is_write(node)
            if verdict is not False:
                return f"{mod}.open({'?' if verdict is None else 'w'})"
            return None
    if isinstance(func, ast.Attribute) and func.attr in PATH_METHOD_SINKS:
        return f".{func.attr}"
    if isinstance(func, ast.Name) and func.id in OPEN_NAMES:
        verdict = _open_mode_is_write(node)
        if verdict is not False:
            return f"open({'?' if verdict is None else 'w'})"
        return None
    return None


def _walk_this_scope(node: ast.AST):
    """``ast.walk``, but PRUNING nested ``def`` / ``class`` / ``lambda``.

    ``ast.walk`` has no way to skip a subtree, so filtering the boundary node
    out of its output still visits everything inside it — a ``raise`` written
    in a nested function would read as "this handler stops". Pruning is the
    whole point, so the traversal is explicit.
    """
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        for child in ast.iter_child_nodes(cur):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            stack.append(child)


def _handler_stops(handler: ast.ExceptHandler) -> bool:
    """Does *handler* end the run instead of swallowing the failure?

    Raise, ``sys.exit`` (or ``parser.error`` / ``_die_on_write_error``), or a
    ``return`` of anything that is not ``EXIT_OK`` / ``0`` / ``None``. A
    nested ``def`` inside the handler does not count — its body runs later.
    """
    for stmt in handler.body:
        if isinstance(stmt, _SCOPE_BOUNDARIES):
            continue
        for node in _walk_this_scope(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call) and _name_of(node.func) in _EXITING_CALLS:
                return True
            if isinstance(node, ast.Return):
                val = node.value
                if val is None:
                    continue
                if isinstance(val, ast.Constant) and val.value in (None, 0):
                    continue
                if isinstance(val, ast.Name) and val.id in _OK_RETURNS:
                    continue
                return True
    return False


def _is_output_write(item: ast.withitem) -> bool:
    ctx = item.context_expr
    return isinstance(ctx, ast.Call) and _name_of(ctx.func) == WRAPPER_NAME


def scan_source(source: str, label: str = "<snippet>") -> list[SinkCall]:
    """Every raw output sink in *source*, each with the guard around it.

    Same walk as ``test_write_failure_class.scan_source``: climb the parent
    chain and STOP at a ``def`` / ``class`` / ``lambda``, because a body
    written inside a ``try`` or a ``with`` runs when it is CALLED, not where
    it is written.
    """
    tree = ast.parse(source, label)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found: list[SinkCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name_of(node.func)
        if name and (name.endswith("_or_die") or name.endswith("_secure")):
            found.append(SinkCall(label, node.lineno, f"{name}()", "or_die"))
            continue
        sink = _classify_sink(node)
        if sink is None:
            continue
        guard: str | None = None
        cur: ast.AST = node
        while cur in parents:
            parent = parents[cur]
            if isinstance(parent, _SCOPE_BOUNDARIES):
                break
            if isinstance(parent, (ast.With, ast.AsyncWith)) and cur in parent.body:
                if any(_is_output_write(i) for i in parent.items):
                    guard = "output_write"
                    break
            if isinstance(parent, ast.Try) and cur in parent.body:
                if any(_handler_can_catch(h) and _handler_stops(h) for h in parent.handlers):
                    guard = "try"
                    break
            cur = parent
        found.append(SinkCall(label, node.lineno, sink, guard))
    # ``ast.walk`` is breadth-first, so the raw order is not source order —
    # every caller here reads the list positionally.
    return sorted(found, key=lambda c: (c.line, c.sink))


def bare_sinks(calls: list[SinkCall], not_guarded: dict[str, str]) -> list[str]:
    """The gate's verdict, as a pure function of scanned sinks + exceptions.

    Split out so the controls below can drive it with synthetic sites: in
    Phase A the real population is empty, and a gate that only ever runs on
    an empty list would be untested code shipping green.
    """
    return [f"  {c.where}  {c.sink}"
            for c in calls if not c.guarded and c.where not in not_guarded]


def stale_exceptions(calls: list[SinkCall], not_guarded: dict[str, str]) -> list[str]:
    """Entries of *not_guarded* that no longer name a real unguarded sink."""
    by_where = {c.where: c for c in calls}
    stale = []
    for where, reason in sorted(not_guarded.items()):
        if where not in by_where:
            stale.append(f"{where}: no sink at that line any more ({reason})")
        elif by_where[where].guarded:
            stale.append(f"{where}: now guarded — delete this entry ({reason})")
        elif not reason.strip():
            stale.append(f"{where}: needs a reason")
    return stale


def scan_file(rel: str) -> list[SinkCall]:
    return scan_source((REPO_ROOT / rel).read_text(encoding="utf-8"), rel)


def scan_tools_tree() -> list[SinkCall]:
    out: list[SinkCall] = []
    for py in sorted(TOOLS_DIR.rglob("*.py")):
        out.extend(scan_source(py.read_text(encoding="utf-8"),
                               py.relative_to(REPO_ROOT).as_posix()))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Scanner controls — synthetic, both signs, born with the mechanism (D-05d)
# ═══════════════════════════════════════════════════════════════════════════
def _guards(src: str) -> list[str | None]:
    return [c.guard for c in scan_source(src)]


class TestSinkVocabulary:
    """Which calls are sinks at all — the classification step, tested on its
    own, because a classifier's worst failure is not judging something."""

    @pytest.mark.parametrize("src,sink", [
        ("open(p, 'w')", "open(w)"),
        ("open(p, 'wb')", "open(w)"),
        ("open(p, 'a')", "open(w)"),
        ("open(p, 'x')", "open(w)"),
        ("open(p, 'r+')", "open(w)"),
        ("open(p, mode='w', encoding='utf-8')", "open(w)"),
        ("io.open(p, 'w')", "io.open(w)"),
        ("Path(p).write_text(s)", ".write_text"),
        ("Path(p).write_bytes(b)", ".write_bytes"),
        ("out.mkdir(parents=True)", ".mkdir"),
        ("out.chmod(0o600)", ".chmod"),
        ("os.makedirs(d, exist_ok=True)", "os.makedirs"),
        ("os.chmod(p, 0o644)", "os.chmod"),
        ("os.replace(tmp, p)", "os.replace"),
        ("os.rename(tmp, p)", "os.rename"),
        ("os.fdopen(fd, 'w')", "os.fdopen"),
        ("tempfile.mkstemp(dir=d)", "tempfile.mkstemp"),
        ("shutil.copy2(src, dst)", "shutil.copy2"),
        ("shutil.move(src, dst)", "shutil.move"),
        ("shutil.copytree(src, dst)", "shutil.copytree"),
    ])
    def test_write_sinks_are_seen(self, src, sink):
        assert [(c.sink, c.guard) for c in scan_source(src)] == [(sink, None)]

    @pytest.mark.parametrize("src", [
        "open(p)",                       # read
        "open(p, 'r')",
        "open(p, encoding='utf-8')",
        "Path(p).read_text()",
        "os.path.exists(p)",
        "os.environ.copy()",             # dict.copy, not shutil.copy
        "policy.copy()",
        "content.replace(old, new)",     # str.replace, not os.replace
        "name.rename_thing()",
    ])
    def test_non_sinks_are_not_seen(self, src):
        assert scan_source(src) == []

    def test_an_unreadable_mode_is_treated_as_a_write(self):
        """Fail LOUD on a site the classifier cannot judge: a variable mode
        reads as a sink, so somebody has to look at it."""
        assert [(c.sink, c.guard) for c in scan_source("open(p, mode)")] == [("open(?)", None)]

    def test_the_measured_blind_spot_is_the_documented_one(self):
        """Pins the false-red trade-off in the module docstring: method-form
        replace/rename are not sinks, os-qualified ones are. If someone
        widens this, this control tells them which claim they invalidated."""
        assert scan_source("tmp.replace(target)") == []
        assert scan_source("src.rename(dst)") == []
        assert [c.sink for c in scan_source("os.replace(tmp, target)")] == ["os.replace"]


class TestGuardShapes:
    def test_bare_sink_is_unguarded(self):
        assert _guards("open(p, 'w').write(x)\n") == [None]

    def test_inside_with_output_write_is_guarded(self):
        src = ("with output_write(p, flag='-o'):\n"
               "    with open(p, 'w') as fh:\n        fh.write(x)\n")
        assert _guards(src) == ["output_write"]

    def test_a_different_context_manager_does_not_guard(self):
        """Only ``output_write``. A `with open(...)` or a `suppress` must not
        read as a guard — that is the cheapest wrong way to turn this green."""
        assert _guards("with contextlib.suppress(OSError):\n    open(p, 'w')\n") == [None]
        assert _guards("with tempfile.TemporaryDirectory() as d:\n    open(p, 'w')\n") == [None]

    def test_output_write_among_several_with_items_still_guards(self):
        src = "with ctx(), output_write(p, flag=None):\n    open(p, 'w')\n"
        assert _guards(src) == ["output_write"]

    def test_a_sink_after_the_with_block_is_not_guarded(self):
        src = ("with output_write(p, flag='-o'):\n    open(p, 'w')\n"
               "open(q, 'w')\n")
        assert _guards(src) == ["output_write", None]

    def test_or_die_and_secure_calls_read_as_already_closed(self):
        assert _guards("write_text_or_die(p, c, flag='-o')\n") == ["or_die"]
        assert _guards("lib.write_json_secure(p, d)\n") == ["or_die"]

    def test_try_with_a_stopping_handler_is_guarded(self):
        for handler in ("except OSError:\n    raise\n",
                        "except OSError as e:\n    sys.exit(2)\n",
                        "except OutputWriteError as e:\n    return EXIT_CALLER_ERROR\n",
                        "except Exception:\n    raise SystemExit(2)\n"):
            assert _guards(f"try:\n    open(p, 'w')\n{handler}") == ["try"], handler

    def test_a_swallowing_handler_is_not_a_guard(self):
        """The ops/da_assembler shape: `except Exception:` + a log line, no
        raise and no exit — measured rc=0 on an unwritable --config-dir. The
        class is 'caught' and the operator is told it worked."""
        for handler in ("except Exception as e:\n    print(f'warn: {e}')\n",
                        "except OSError:\n    pass\n",
                        "except OSError as e:\n    log.warning(e)\n    return EXIT_OK\n",
                        "except OSError:\n    return 0\n",
                        "except OSError:\n    return\n"):
            assert _guards(f"try:\n    open(p, 'w')\n{handler}") == [None], handler

    def test_a_narrow_handler_is_not_a_guard(self):
        src = "try:\n    open(p, 'w')\nexcept ValueError:\n    raise\n"
        assert _guards(src) == [None]

    def test_a_sink_in_a_handler_body_is_not_guarded_by_its_own_try(self):
        src = "try:\n    pass\nexcept OSError:\n    open(p, 'w')\n    raise\n"
        assert _guards(src) == [None]

    def test_a_sink_in_a_nested_def_is_not_guarded_by_the_outer_try(self):
        """A `try` around a `def` guards nothing: the body runs when called."""
        src = ("try:\n    def _w():\n        open(p, 'w')\n"
               "except OSError:\n    raise\n")
        assert _guards(src) == [None]

    def test_a_sink_in_a_nested_def_is_not_guarded_by_the_outer_with(self):
        src = ("with output_write(p, flag='-o'):\n"
               "    def _w():\n        open(p, 'w')\n")
        assert _guards(src) == [None]

    def test_a_lambda_body_is_not_guarded_either(self):
        src = "with output_write(p, flag='-o'):\n    f = lambda: Path(p).write_text(s)\n"
        assert _guards(src) == [None]

    def test_a_raise_inside_a_nested_def_does_not_make_a_handler_stop(self):
        src = ("try:\n    open(p, 'w')\n"
               "except OSError:\n    def later():\n        raise\n")
        assert _guards(src) == [None]

    def test_nesting_output_write_inside_a_swallowing_try_still_guards(self):
        """The wrapper is judged on its own: an outer try that swallows does
        not un-guard a wrapped sink (it changes what the tool DOES with the
        error, which is the behavioural gate's job, not this one's)."""
        src = ("try:\n    with output_write(p, flag='-o'):\n        open(p, 'w')\n"
               "except OSError:\n    pass\n")
        assert _guards(src) == ["output_write"]


def test_this_scanner_agrees_with_the_secure_writer_one_where_they_overlap():
    """Reuse check, per axis (rulebook D-05a): the imported pieces really are
    the same judgement, not a second copy that drifts.

    Both scanners answer "is this call lexically inside a catching try,
    without crossing a def". Fed the same shapes, they must agree — with the
    ONE documented difference: a handler that catches but does not stop is
    guarded there and unguarded here.
    """
    assert _handler_can_catch is _wfc._handler_can_catch
    assert CATCHING_HANDLER_NAMES is _wfc.CATCHING_HANDLER_NAMES
    assert _SCOPE_BOUNDARIES is _wfc._SCOPE_BOUNDARIES

    agree = [
        "try:\n    {call}\nexcept OSError:\n    raise\n",
        "try:\n    {call}\nexcept ValueError:\n    raise\n",
        "try:\n    def f():\n        {call}\nexcept OSError:\n    raise\n",
        "try:\n    pass\nexcept OSError:\n    {call}\n    raise\n",
    ]
    for tpl in agree:
        mine = scan_source(tpl.format(call="open(p, 'w')"))
        theirs = _secure_writer_scan(tpl.format(call="write_text_secure(p, c)"))
        assert [c.guarded for c in mine] == [c.guarded for c in theirs], tpl

    # The one deliberate divergence, pinned so it cannot become accidental.
    swallow = "try:\n    {call}\nexcept OSError:\n    pass\n"
    assert _guards(swallow.format(call="open(p, 'w')")) == [None]
    assert [c.guarded for c in _secure_writer_scan(
        swallow.format(call="write_text_secure(p, c)"))] == [True]


# ═══════════════════════════════════════════════════════════════════════════
# Self-check — the instrument must find known sites in the real tree
#
# Every control above is synthetic, so all of them could pass against a
# scanner pointed at nothing (rulebook D-07d: a broken scanner and a clean
# tree both report zero). These read the tree itself.
# ═══════════════════════════════════════════════════════════════════════════
_KNOWN_SITES = {
    # file → sinks that must still be found there. Measured on this tree;
    # each is a real argv-fed output path from the #1789 survey.
    "scripts/tools/dx/describe_tenant.py": {".write_text"},
    "scripts/tools/ops/baseline_discovery.py": {"os.makedirs", "open(w)"},
    "scripts/tools/ops/assemble_config_dir.py": {"shutil.copy2", "os.chmod"},
    "scripts/tools/ops/state_reconcile.py": {"tempfile.mkstemp", "os.fdopen", "os.replace"},
    "scripts/tools/dx/run_chaos_soak.py": {".mkdir", "open(w)"},
}


@pytest.mark.parametrize("rel,expected", sorted(_KNOWN_SITES.items()))
def test_scanner_finds_the_known_sites(rel, expected):
    """Deleting a sink from the vocabulary, or pointing the scan at the wrong
    root, makes this red — where the synthetic controls above would not."""
    assert (REPO_ROOT / rel).is_file(), rel
    seen = {c.sink for c in scan_file(rel)}
    assert expected <= seen, f"{rel}: missing {sorted(expected - seen)}; saw {sorted(seen)}"


def test_the_scan_root_is_not_empty():
    """Anti-vacuity for the instrument, NOT for GUARDED_FILES: it is taken
    from the whole tools tree, which does not shrink when the pinned list
    does. A drifted scan root reports zero, and zero is what a clean tree
    reports too."""
    sinks = [c for c in scan_tools_tree() if c.guard != "or_die"]
    assert len(sinks) >= 100, (
        f"only {len(sinks)} raw sinks found under {TOOLS_DIR} — the scan root "
        "or the sink vocabulary has drifted")
    assert any(c.guard == "try" for c in sinks), "no try-guarded sink found — guard arm dead"


# ═══════════════════════════════════════════════════════════════════════════
# Gate controls — the verdict logic, driven with synthetic sites
#
# ``GUARDED_FILES`` is empty in Phase A, so the gate below runs on an empty
# list and can only say yes. These drive the same two functions it calls with
# a population that is not empty, so the machinery Phase B switches on is
# already known to work — and known to be able to say NO.
# ═══════════════════════════════════════════════════════════════════════════
_UNWRAPPED = "def main():\n    out.mkdir(parents=True)\n    Path(p).write_text(s)\n"
_WRAPPED = ("def main():\n"
            "    with output_write(out, flag='-o'):\n        out.mkdir(parents=True)\n"
            "    with output_write(p, flag='-o'):\n        Path(p).write_text(s)\n")


class TestGateVerdict:
    def test_an_unwrapped_file_is_reported(self):
        calls = scan_source(_UNWRAPPED, "scripts/tools/dx/fake_tool.py")
        offenders = bare_sinks(calls, {})
        assert len(offenders) == 2, offenders
        assert all("scripts/tools/dx/fake_tool.py:" in o for o in offenders)

    def test_a_wrapped_file_is_clean(self):
        assert bare_sinks(scan_source(_WRAPPED, "scripts/tools/dx/fake_tool.py"), {}) == []

    def test_removing_one_with_reopens_the_gate(self):
        """The #1789 counterfactual in miniature: drop a single ``with`` line
        and exactly one site comes back."""
        one_less = _WRAPPED.replace(
            "    with output_write(out, flag='-o'):\n        out.mkdir(parents=True)\n",
            "    out.mkdir(parents=True)\n")
        offenders = bare_sinks(scan_source(one_less, "scripts/tools/dx/fake_tool.py"), {})
        assert len(offenders) == 1 and ".mkdir" in offenders[0], offenders

    def test_a_listed_exception_silences_exactly_its_own_line(self):
        calls = scan_source(_UNWRAPPED, "scripts/tools/dx/fake_tool.py")
        listed = {"scripts/tools/dx/fake_tool.py:2": "long-lived handle, closed elsewhere"}
        offenders = bare_sinks(calls, listed)
        assert len(offenders) == 1 and ".write_text" in offenders[0], offenders

    def test_an_exception_on_a_line_that_healed_is_stale(self):
        calls = scan_source(_WRAPPED, "scripts/tools/dx/fake_tool.py")
        stale = stale_exceptions(calls, {"scripts/tools/dx/fake_tool.py:3": "was bare"})
        assert stale and "now guarded" in stale[0], stale

    def test_an_exception_on_a_line_that_vanished_is_stale(self):
        calls = scan_source(_UNWRAPPED, "scripts/tools/dx/fake_tool.py")
        stale = stale_exceptions(calls, {"scripts/tools/dx/fake_tool.py:99": "gone"})
        assert stale and "no sink at that line" in stale[0], stale

    def test_an_exception_without_a_reason_is_stale(self):
        calls = scan_source(_UNWRAPPED, "scripts/tools/dx/fake_tool.py")
        stale = stale_exceptions(calls, {"scripts/tools/dx/fake_tool.py:2": "   "})
        assert stale and "needs a reason" in stale[0], stale


# ═══════════════════════════════════════════════════════════════════════════
# The gate — vacuous in Phase A, and it says so
# ═══════════════════════════════════════════════════════════════════════════
def test_guarded_files_ceiling_is_explicit():
    """Exact size, not `<=`: adding a file to the pinned population is a
    decision that must appear in the diff next to the sites it pins.

    Phase A ships this at 0. A green run of the gate below therefore proves
    NOTHING about the tree yet — this constant is what stops that being
    mistaken for coverage.
    """
    assert len(GUARDED_FILES) == _GUARDED_FILES_CEILING, GUARDED_FILES
    assert len(NOT_GUARDED) == _NOT_GUARDED_CEILING, sorted(NOT_GUARDED)


def test_guarded_files_are_real_and_have_sinks():
    """Exit-lock: a pinned file must exist and still hold at least one sink,
    so the list can only shrink deliberately."""
    stale = []
    for rel in GUARDED_FILES:
        if not (REPO_ROOT / rel).is_file():
            stale.append(f"{rel}: file no longer exists")
        elif not scan_file(rel):
            stale.append(f"{rel}: no raw sink left — remove it from GUARDED_FILES")
    assert not stale, "\n".join(stale)


def test_not_guarded_entries_point_at_real_unguarded_sinks():
    """Exit-lock on the exceptions: every entry must name a site that is
    (a) inside a pinned file and (b) actually still unguarded. An entry that
    got fixed must be deleted, so the exception list only shrinks."""
    calls = [c for rel in GUARDED_FILES for c in scan_file(rel)]
    assert not stale_exceptions(calls, NOT_GUARDED), "\n".join(
        stale_exceptions(calls, NOT_GUARDED))


def test_no_raw_sink_in_a_guarded_file_is_bare():
    """Every raw sink in a #1789 file is wrapped, closed, or listed.

    Fix an offender by wrapping it — ``with output_write(path, flag=...)``
    around the raw operation — not by adding it to ``NOT_GUARDED``. That list
    is for sinks that CANNOT be wrapped (a long-lived handle whose write and
    close happen elsewhere, a tempfile under a path the operator never named);
    each entry costs a line of reasoning and is re-checked above.
    """
    offenders = bare_sinks([c for rel in GUARDED_FILES for c in scan_file(rel)],
                           NOT_GUARDED)
    assert not offenders, (
        f"{len(offenders)} unguarded raw output sink(s) in #1789 files:\n"
        + "\n".join(offenders))
