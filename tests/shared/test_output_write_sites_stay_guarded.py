"""Static pin: a RAW output sink in a #1789 file stays guarded.

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
   ``OutputWriteError`` **and actually ends the run the right way** — the
   handler body calls ``sys.exit`` / ``os._exit`` (or
   ``_die_on_write_error`` / ``parser.error``), returns something other than
   ``EXIT_OK``, or raises a NEW exception object
   (``raise OutputWriteError(...)``). ⛔ Judged on the DOTTED name:
   ``log.error(exc)`` is a swallow, not an exit.

Arm 3 is deliberately STRICTER than the one in ``test_write_failure_class``,
in TWO measured ways, and both are pinned by
``test_this_scanner_agrees_with_the_secure_writer_one_where_they_overlap``:

* **A handler that swallows is not a guard.** ``ops/da_assembler`` measured
  rc=0 on an unwritable ``--config-dir`` because its ``except Exception:``
  logged and carried on: the write-failure class was "caught" and the
  operator got a success. That is the other failure mode of the same defect.
* **A BARE ``raise`` (or ``raise e``, the same object) is not a guard here**
  (#1789 group 3). This population's sinks raise a plain ``OSError``; a bare
  re-raise passes that ``OSError`` outwards UNCHANGED, and the thing waiting
  at the top — ``exit_on_output_write_error`` — catches only
  ``OutputWriteError``. So the operator still gets a traceback at rc=1: the
  handler stopped the run, but not in the way this ticket is about. In
  ``test_write_failure_class``'s population the same shape is genuinely a
  guard, because there the exception in flight ALREADY IS an
  ``OutputWriteError`` (the secure writer made it one) and re-raising it
  reaches ``_or_die`` / the decorator intact.

  Measured: ``ops/state_reconcile.write_json`` cleans its temp file up in an
  ``except BaseException: … raise``. Under the old arm its ``os.fdopen`` /
  ``os.replace`` read as guarded whether or not the ``with output_write(...)``
  around them existed — deleting the wrapper left this gate GREEN. It does
  not any more.

THE SCANNER
-----------
Reuses ``test_write_failure_class``: the same handler predicate
(``_handler_can_catch`` over ``CATCHING_HANDLER_NAMES``) and the same scope
boundaries (``_SCOPE_BOUNDARIES``), imported rather than restated, plus a
control that its walk-up rule and this one agree on the shapes they share.

⚠️ **Measured blind spot, on purpose.** ``Path.replace`` / ``Path.rename``
(method form) are NOT sinks here, while ``os.replace`` / ``os.rename`` are.
Reason: 174 ``.replace(`` call sites under ``scripts/tools/`` and only 3 of
them are ``os.``-qualified; the rest are ``str.replace`` and
``datetime.replace``. A predicate with that false-red
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

# ⛔ Matched on the FULL dotted spelling, never on the trailing attribute.
# `_name_of` (the sink vocabulary's helper) answers the tail, and a tail-only
# vocabulary of {"exit", "error", "fail", …} scored `log.error(exc)` as a
# handler that ENDS THE RUN — the cheapest possible way to turn this gate
# green while leaving the operator with a swallowed write failure. Same for
# `writer.fail()` and any `obj.exit()`. Both directions are pinned by the
# synthetic controls below.
_EXITING_CALLS = frozenset({"sys.exit", "os._exit", "_die_on_write_error"})
# `parser.error(...)` really does end the run (argparse's `error` exits 2),
# but only when the receiver IS a parser. There is no type information here,
# so the receiver's NAME is the evidence — deliberately narrow, and a
# false-red (an argument parser called something else) is the safe side:
# someone has to look (rulebook D-05g).
_PARSER_RECEIVER_SUFFIXES = ("parser", "ap", "argp")

# ── The population ─────────────────────────────────────────────────────────
# ⛔ Repo-relative POSIX paths of the tool files this ticket wraps. A file
# lands here in the SAME diff that wraps its sites and raises the ceiling
# below — the ceiling is what stops a file being added without anyone
# counting, and what stops a green run from being read as tree-wide
# coverage. Everything NOT in this tuple is unpinned and this file knows
# nothing about it.
GUARDED_FILES: tuple[str, ...] = (
    "scripts/tools/dx/compile_custom_alerts.py",
    "scripts/tools/dx/describe_tenant.py",
    "scripts/tools/dx/generate_tenant_fixture.py",
    "scripts/tools/dx/generate_tenant_metadata.py",
    "scripts/tools/dx/migrate_conf_d.py",
    "scripts/tools/dx/paired_trend_watch.py",
    "scripts/tools/dx/pair_bench_ratio.py",
    "scripts/tools/dx/render_soak_diff.py",
    "scripts/tools/dx/run_chaos_soak.py",
    "scripts/tools/dx/scan_component_health.py",
    "scripts/tools/dx/write_baseline_marker.py",
    "scripts/tools/lint/trufflehog_to_sarif.py",
    "scripts/tools/ops/assemble_config_dir.py",
    "scripts/tools/ops/baseline_discovery.py",
    "scripts/tools/ops/blast_radius.py",
    "scripts/tools/ops/config_history.py",
    "scripts/tools/ops/da_assembler.py",
    "scripts/tools/ops/federation_keygen.py",
    "scripts/tools/ops/generate_rule_pack_split.py",
    "scripts/tools/ops/state_reconcile.py",
)
_GUARDED_FILES_CEILING = 20

# Sites inside a GUARDED_FILES file that stay unguarded on purpose, as
# `"<repo-relative path>:<line>"` → reason. Exit-locked the same way: an
# entry that no longer names an unguarded sink must be REMOVED, so the list
# only shrinks.
NOT_GUARDED: dict[str, str] = {
    "scripts/tools/dx/run_chaos_soak.py:262":
        "`trigger_reload`'s carrier perturbation. It writes into the "
        "`--config-dir` INPUT tree — that toggle IS the chaos the soak "
        "applies — and its `except OSError: continue` is the intended "
        "behaviour: an unwritable carrier means 'perturb the next one', not "
        "'the operator mistyped a flag'. Wrapping it would blame "
        "`--output-dir` for a failure on a path that flag never named.",
    "scripts/tools/ops/generate_rule_pack_split.py:154":
        "_safe_mkdir's no-_lib_python fallback. It runs only when the "
        "`from _lib_python import ...` at the top of that file raised "
        "ImportError, which (measured) means PyYAML is missing — and "
        "`_lib_io`, where output_write lives, does a bare `import yaml`, so "
        "the wrapper is unavailable in exactly this case. Wrapping it would "
        "turn today's graceful 'PyYAML missing => per-pack error, rc=2' into "
        "an ImportError traceback at rc=1 before argparse runs. Unlike "
        "_safe_write's fallback (deleted in #1789 as unreachable), this arm "
        "IS reached: it creates edge-rules/ and central-rules/ before any "
        "pack is parsed.",
}
_NOT_GUARDED_CEILING = 2

# ⚠️ **What NOT_GUARDED cannot hold, so it is written here instead.** The list
# above is exit-locked against the SCANNER: every entry must name a line the
# scanner reports as an unguarded sink, or `test_not_guarded_entries_point_at_
# real_unguarded_sinks` calls it stale. That means an unwrapped write which is
# not in the sink VOCABULARY cannot be listed at all — it is invisible in both
# directions. The known ones are all in `dx/run_chaos_soak`, on the
# long-lived CSV handle it holds open for the length of the soak:
#
#   * `writer.writerow(...)` + `csv_file.flush()` in the poll loop, and
#   * `csv_file.close()` in the `finally`, where the last flush happens.
#
# `open()` is wrapped (the failure an operator actually causes — a bad
# `--output-dir` — happens there); a write or a flush that fails LATER means
# the filesystem filled up or went away mid-run, and it still ends in a
# traceback at rc=1. Wrapping them would need the wrapper to span the whole
# soak, which would also catch every unrelated OSError in the loop. Recorded
# because "not in the list" must not read as "not there"; the same is true of
# `ops/config_history`'s deliberately unguarded READ.


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


def _raise_makes_a_new_exception(node: ast.Raise, handler: ast.ExceptHandler) -> bool:
    """Does this ``raise`` put a DIFFERENT exception on the wire?

    The distinction arm 3 turns on (#1789 group 3). ``raise`` on its own, and
    ``raise e`` naming the handler's own bound exception, both send the
    ORIGINAL object outwards. For this file's population that object is a raw
    ``OSError``, and nothing above catches a raw ``OSError`` — the tool still
    ends in a traceback at rc=1. Anything else (``raise OutputWriteError(...)``,
    ``raise SystemExit(2)``, a new object held in a variable) is a real
    conversion, and IS what closes the site.

    ⚠️ Deliberately syntactic, and it can be fooled: ``raise e`` where the
    handler rebound ``e`` to a converted exception first would read as a bare
    re-raise. That direction is a FALSE RED — someone has to look — which is
    the safe side for a gate (rulebook D-05g).
    """
    if node.exc is None:
        return False                      # bare `raise`
    if isinstance(node.exc, ast.Name) and handler.name and node.exc.id == handler.name:
        return False                      # `raise e` — the same object
    return True


def _dotted_name(func: ast.expr) -> str | None:
    """The call target as WRITTEN: ``sys.exit``, ``log.error``, ``error``.

    :func:`_name_of` answers the trailing attribute, which is what the sink
    vocabulary wants (``os.replace`` and ``Path.replace`` are judged
    separately there). For the handler predicate the receiver is the whole
    question, so this keeps it.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _dotted_name(func.value)
        return f"{base}.{func.attr}" if base else None
    return None


def _call_ends_the_run(node: ast.Call) -> bool:
    """Does this call end the run? See ``_EXITING_CALLS`` for why it is
    spelled dotted."""
    dotted = _dotted_name(node.func)
    if dotted is None:
        return False
    if dotted in _EXITING_CALLS:
        return True
    receiver, _, attr = dotted.rpartition(".")
    if attr != "error" or not receiver:
        return False
    return receiver.rsplit(".", 1)[-1].lower().endswith(_PARSER_RECEIVER_SUFFIXES)


def _handler_stops(handler: ast.ExceptHandler) -> bool:
    """Does *handler* end the run the way this ticket's contract needs?

    ``sys.exit`` / ``os._exit`` (or ``parser.error`` / ``_die_on_write_error``
    — see :func:`_call_ends_the_run`), a ``return`` of anything that is not
    ``EXIT_OK`` / ``0`` / ``None``, or a ``raise`` of a NEW exception object —
    see :func:`_raise_makes_a_new_exception` for why a bare re-raise does not
    count. A nested ``def`` inside the handler does not count either — its
    body runs later.
    """
    for stmt in handler.body:
        if isinstance(stmt, _SCOPE_BOUNDARIES):
            continue
        for node in _walk_this_scope(stmt):
            if isinstance(node, ast.Raise):
                if _raise_makes_a_new_exception(node, handler):
                    return True
                continue
            if isinstance(node, ast.Call) and _call_ends_the_run(node):
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

    Split out so the controls below can drive it with synthetic sites: the
    real population is whatever ``GUARDED_FILES`` currently holds, and a gate
    that only ever ran on files that happen to be clean could not be shown to
    say NO.
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
        for handler in ("except OSError as e:\n    sys.exit(2)\n",
                        "except OutputWriteError as e:\n    return EXIT_CALLER_ERROR\n",
                        "except Exception:\n    raise SystemExit(2)\n",
                        # A raise that CONVERTS: a new exception object, which
                        # `exit_on_output_write_error` can act on.
                        "except OSError as e:\n    raise OutputWriteError(p, e, flag='-o') from e\n",
                        "except OSError:\n    raise RuntimeError('nope')\n"):
            assert _guards(f"try:\n    open(p, 'w')\n{handler}") == ["try"], handler

    def test_a_logger_call_is_not_an_exit(self):
        """⛔ The cheapest false green there is. `log.error(exc)` shares its
        trailing attribute with `parser.error(...)`, so a tail-only predicate
        scored a handler that only LOGS as one that ends the run — the exact
        shape (`ops/da_assembler`) this file exists to catch.

        A specific break that reddens this: put the bare tails ("error",
        "fail", "exit") back into `_EXITING_CALLS`.
        """
        for handler in ("except OSError as e:\n    log.error(e)\n",
                        "except OSError as e:\n    logger.error('write failed: %s', e)\n",
                        "except OSError as e:\n    self.log.error(e)\n",
                        "except OSError as e:\n    writer.fail(e)\n",
                        "except OSError as e:\n    ui.exit(e)\n"):
            assert _guards(f"try:\n    open(p, 'w')\n{handler}") == [None], handler

    def test_a_logger_call_before_an_ok_return_is_not_an_exit_either(self):
        src = ("def main():\n    try:\n        open(p, 'w')\n"
               "    except OSError as e:\n        logger.error(e)\n"
               "        return EXIT_OK\n")
        assert _guards(src) == [None]

    def test_the_real_exits_still_count(self):
        """The complement, so the narrowing is "the receiver matters", not
        "nothing counts any more"."""
        for handler in ("except OSError as e:\n    sys.exit(2)\n",
                        "except OSError as e:\n    os._exit(2)\n",
                        "except OSError as e:\n    parser.error(str(e))\n",
                        "except OSError as e:\n    self.parser.error(str(e))\n",
                        "except OSError as e:\n    ap.error(str(e))\n",
                        "except OutputWriteError as e:\n    _die_on_write_error(e, 2)\n"):
            assert _guards(f"try:\n    open(p, 'w')\n{handler}") == ["try"], handler

    def test_a_bare_reraise_is_not_a_guard(self):
        """#1789 group 3. A bare ``raise`` sends the ORIGINAL exception on, and
        for this population that is a raw ``OSError`` — which nothing above
        catches, so the operator still gets a traceback at rc=1.

        The real site: ``ops/state_reconcile.write_json`` wraps its atomic
        write in ``except BaseException: <unlink temp>; raise`` to clean up.
        That cleanup is right and stays; what it must NOT do is read as this
        ticket's guard, because with the ``with output_write(...)`` deleted the
        run goes straight back to a traceback.
        """
        for handler in ("except OSError:\n    raise\n",
                        "except BaseException:\n    os.unlink(tmp)\n    raise\n",
                        # Same object under a name — a re-raise wearing a hat.
                        "except OSError as e:\n    raise e\n",
                        "except OSError as e:\n    log.warning(e)\n    raise e\n"):
            assert _guards(f"try:\n    open(p, 'w')\n{handler}") == [None], handler

    def test_a_raise_of_a_different_name_still_counts(self):
        """The complement, so the rule is "the same object", not "any Name":
        a handler that raises something it built earlier is converting."""
        src = ("try:\n    open(p, 'w')\n"
               "except OSError as e:\n    wrapped = OutputWriteError(p, e, flag='-o')\n"
               "    raise wrapped\n")
        assert _guards(src) == ["try"]

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
    TWO documented differences, both pinned below: a handler that catches but
    does not stop, and a handler whose only action is a BARE ``raise``, are
    each guarded there and unguarded here.
    """
    assert _handler_can_catch is _wfc._handler_can_catch
    assert CATCHING_HANDLER_NAMES is _wfc.CATCHING_HANDLER_NAMES
    assert _SCOPE_BOUNDARIES is _wfc._SCOPE_BOUNDARIES

    # Written with a CONVERTING raise, not a bare one: a bare `raise` is now
    # one of the deliberate divergences below, and using it here would make
    # these templates agree for a reason that has nothing to do with the axis
    # they are testing (scope boundaries and the handler-catch predicate).
    agree = [
        "try:\n    {call}\nexcept OSError:\n    raise SystemExit(2)\n",
        "try:\n    {call}\nexcept ValueError:\n    raise SystemExit(2)\n",
        "try:\n    def f():\n        {call}\nexcept OSError:\n    raise SystemExit(2)\n",
        "try:\n    pass\nexcept OSError:\n    {call}\n    raise SystemExit(2)\n",
    ]
    for tpl in agree:
        mine = scan_source(tpl.format(call="open(p, 'w')"))
        theirs = _secure_writer_scan(tpl.format(call="write_text_secure(p, c)"))
        assert [c.guarded for c in mine] == [c.guarded for c in theirs], tpl

    # ── The deliberate divergences, pinned so neither becomes accidental ──
    # (1) A handler that catches but SWALLOWS. Guarded there, not here: the
    #     write-failure class being "handled" is not the same as the operator
    #     being told (ops/da_assembler measured rc=0).
    swallow = "try:\n    {call}\nexcept OSError:\n    pass\n"
    assert _guards(swallow.format(call="open(p, 'w')")) == [None]
    assert [c.guarded for c in _secure_writer_scan(
        swallow.format(call="write_text_secure(p, c)"))] == [True]

    # (2) #1789 group 3 — a BARE re-raise. Guarded there, not here, and the
    #     asymmetry is in the POPULATIONS, not in either scanner's taste:
    #
    #       there  the sink is a secure writer, so the exception in flight is
    #              ALREADY an OutputWriteError; `raise` hands it to `_or_die` /
    #              `exit_on_output_write_error` intact ⇒ still rc=2, one line.
    #       here   the sink is raw, so the exception in flight is a plain
    #              OSError; `raise` hands THAT outwards, and the decorator
    #              catches only OutputWriteError ⇒ traceback at rc=1.
    #
    #     Measured on ops/state_reconcile: with the old arm, deleting the
    #     `with output_write(...)` around its atomic write left this gate
    #     green because the temp-file cleanup's `except BaseException: … raise`
    #     answered for it. It goes red now.
    #
    #     ⛔ The fix belongs on THIS side. `test_write_failure_class` is not
    #     changed: for its population a bare re-raise really is a guard, and
    #     tightening it there would be a false red on every secure-writer site
    #     that re-raises for a caller to handle.
    bare = "try:\n    {call}\nexcept OSError:\n    raise\n"
    assert _guards(bare.format(call="open(p, 'w')")) == [None]
    assert [c.guarded for c in _secure_writer_scan(
        bare.format(call="write_text_secure(p, c)"))] == [True]


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
# The real population is whatever ``GUARDED_FILES`` currently holds, and a
# gate that only ever ran on files that happen to be clean could not be shown
# to say NO. These drive the same two functions it calls with sites that are
# deliberately unguarded, so the verdict logic is exercised in both signs
# independently of what the tree looks like today.
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
# The gate — over the pinned population, whose size is stated out loud
# ═══════════════════════════════════════════════════════════════════════════
def test_guarded_files_ceiling_is_explicit():
    """Exact size, not `<=`: adding a file to the pinned population is a
    decision that must appear in the diff next to the sites it pins.

    The number is also the honest scope of a green run below: it proves the
    sinks in exactly this many files are wrapped, and nothing about the rest
    of the tree. This constant is what stops that being mistaken for
    coverage.
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
