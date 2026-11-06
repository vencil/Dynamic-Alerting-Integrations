#!/usr/bin/env python3
"""Write the parts of a tenant rule-pack alert that are derived from its labels (#3309).

One property, one human-written spelling: every tenant rule-pack alert carries a
required label ``maintenance: mute | page``, and that label is the only place the
decision is written. Everything that follows from it is written here:

  * ``mute`` -> the alert's expr ends in the maintenance clause, spelled exactly as
    the custom-alert compiler spells it (``MAINTENANCE_TAIL``; the Go constant
    ``maintenanceTail`` in custom_alert_compile.go, pinned equal by a test). While
    ``_state_maintenance`` is enabled for the tenant the rule does not fire at all.
  * ``page`` -> no maintenance clause anywhere in the alert or the recording rules
    it reads: the alert fires in a maintenance window.

The recurring-maintenance silences (maintenance_scheduler.py) match the same label
(``maintenance="mute"``), so a one-shot and a recurring window mute the same alerts.

Recording rules in the hand-written packs never carry the clause: it lives at the
alert, where the label is. (The compiled custom-alert pack,
rule-pack-custom-alerts.yaml, is the Go compiler's output: its ``:core`` records
carry the tail and every alert is ``mute``. It is checked here, never written.)

Writing
-------
The rule packs are edited as text so comments and layout survive. Only an expr
whose derived form differs is rewritten; it becomes a ``|`` block holding the
body (the expr without its maintenance clause, dedented) and, for ``mute``, the
two-line tail. A body whose top level has an ``or`` is parenthesised first:
PromQL's ``or`` binds looser than ``unless``, so ``A or B unless M`` would mute
only ``B``. A clause wrapped in one pair of parentheses together with the rest
of the expr (``( (X) unless M )``) has that pair removed with it.

The label is not derived: an alert without it, or with another value, is an
error the author has to decide (rc 1), in both modes.

Checking (``--check``)
----------------------
  1. Nothing would be rewritten (a hand-edited or missing tail is drift).
  2. Read the way Prometheus' rule loader reads the files (``da-read
     prometheus-rules``, ADR-036 §12), every tenant alert's label is ``mute`` or
     ``page``, and the alert plus the recording rules it reads (transitively)
     hold exactly one canonical clause for ``mute`` and none for ``page``.
  3. Every ``filter="maintenance"`` spelling is the canonical clause: a variant
     (``=~``, ``== bool 1``, another join) is refused instead of being missed.

Exit codes: 0 ok / wrote, 1 drift or a violation, 2 caller error (a pack the
loader refuses, an expr shape this tool does not understand).

Usage:
  generate_rulepack_derived.py            # rewrite what is stale
  generate_rulepack_derived.py --check    # verify only
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
import _lib_consumer_read  # noqa: E402  (#3206: rule files as Prometheus reads them)
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)

try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

#: The label that carries the decision, and its values.
MAINTENANCE_LABEL = "maintenance"
MUTE = "mute"
PAGE = "page"
VALUES = (MUTE, PAGE)

#: The one spelling of the maintenance clause (= Go's maintenanceTail).
MAINTENANCE_TAIL = 'unless on(tenant)\n(user_state_filter{filter="maintenance"} == 1)'

#: The clause in any whitespace layout — what is recognised and removed.
_CLAUSE = re.compile(
    r'\s*unless\s+on\s*\(\s*tenant\s*\)\s*\(\s*user_state_filter\s*\{\s*filter\s*=\s*"maintenance"\s*\}'
    r'\s*==\s*1\s*\)')
#: Any reference to the maintenance filter, canonical or not.
_ANY_MAINTENANCE = re.compile(r'filter\s*!?[=~]+\s*"maintenance"')
_OR = re.compile(r'(?<![\w:])or(?![\w:])')
_TOKEN = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')

_ITEM = re.compile(r'^(\s*)- (alert|record):\s*(\S.*?)\s*$')
_KEY = re.compile(r'^(\s*)([A-Za-z_][\w-]*):(.*)$')

CUSTOM_ALERTS_PACK = "custom-alerts"


class ShapeError(ValueError):
    """An expr this tool cannot rewrite safely; a human has to look."""


# ── PromQL text helpers ──────────────────────────────────────────────────────

def _mask(expr: str) -> str:
    """Blank quoted strings and ``#`` comments (position-preserving) so paren
    depth and ``or`` scans cannot be fooled by their contents."""
    out: list[str] = []
    quote = ""
    esc = False
    comment = False
    for ch in expr:
        if comment:
            if ch == "\n":
                comment = False
                out.append(ch)
            else:
                out.append(" ")
            continue
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
                out.append(ch)
                continue
            out.append(" ")
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
        elif ch == "#":
            comment = True
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _top_level_or(expr: str) -> bool:
    masked = _mask(expr)
    depth = 0
    for i, ch in enumerate(masked):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and _OR.match(masked, i) and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in "_:")):
            return True
    return False


def _opening_paren_is_unclosed(text: str) -> bool:
    """True when *text* starts with ``(`` that no later character closes."""
    masked = _mask(text)
    if not masked.startswith("("):
        return False
    depth = 0
    for ch in masked:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return False
    return depth == 1


def _clean(text: str) -> str:
    lines = [ln.rstrip() for ln in textwrap.dedent(text).split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _clauses(expr: str) -> list[tuple[int, int]]:
    """Spans of the canonical clauses that are code, not comment or string text."""
    masked = _mask(expr)
    return [m.span() for m in _CLAUSE.finditer(expr)
            if "user_state_filter" in masked[m.start():m.end()]]


def clause_count(expr: str) -> int:
    """Canonical clauses in *expr*; raises ShapeError on a non-canonical spelling."""
    masked = _mask(expr)
    canonical = _clauses(expr)
    for m in _ANY_MAINTENANCE.finditer(expr):
        if masked[m.start():m.end()].strip() == "":
            continue  # inside a comment or string
        if not any(s <= m.start() < e for s, e in canonical):
            raise ShapeError(f"non-canonical maintenance reference near {expr[m.start():m.start() + 40]!r}"
                             f" — spell it as the tail: {MAINTENANCE_TAIL!r}")
    return len(canonical)


def strip_clause(expr: str) -> str:
    """*expr* without its (at most one) maintenance clause, cleaned."""
    n = clause_count(expr)
    if n == 0:
        return _clean(expr)
    if n > 1:
        raise ShapeError(f"{n} maintenance clauses; one alert holds at most one")
    start, end = _clauses(expr)[0]
    before, after = expr[:start], expr[end:]
    if not after.strip():
        return _clean(before)
    if _mask(after).strip() == ")":
        body = before.strip()
        if _opening_paren_is_unclosed(body):
            return _clean(body[1:])
    raise ShapeError("the maintenance clause is not at the end of the expr "
                     "(nor wrapped together with the whole expr in one pair of parentheses)")


def derive_expr(expr: str, mute: bool) -> str:
    """The expr as the label says it must be (see the module docstring)."""
    body = strip_clause(expr)
    if not mute:
        return body
    if _top_level_or(body):
        body = "(\n" + textwrap.indent(body, "  ") + "\n)"
    return body + "\n" + MAINTENANCE_TAIL


# ── rule-pack text model ─────────────────────────────────────────────────────

class Item:
    """One ``- alert:`` / ``- record:`` entry located in a file's lines."""

    def __init__(self, kind: str, name: str, start: int, end: int, indent: int):
        self.kind, self.name, self.start, self.end, self.indent = kind, name, start, end, indent
        self.expr_line: int | None = None
        self.expr_end: int | None = None      # exclusive
        self.expr_text: str | None = None
        self.label: str | None = None
        self.label_line: int | None = None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_items(lines: list[str]) -> list[Item]:
    items: list[Item] = []
    starts = [(i, _ITEM.match(ln)) for i, ln in enumerate(lines)]
    starts = [(i, m) for i, m in starts if m]
    for n, (i, m) in enumerate(starts):
        indent = len(m.group(1))
        end = len(lines)
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if s.strip() and not s.lstrip().startswith("#") and _indent(s) <= indent:
                end = j
                break
        if n + 1 < len(starts) and starts[n + 1][0] < end:
            end = starts[n + 1][0]
        it = Item(m.group(2), _unquote(m.group(3)), i, end, indent)
        _locate_fields(lines, it)
        items.append(it)
    return items


def _locate_fields(lines: list[str], it: Item) -> None:
    key_indent = it.indent + 2
    j = it.start
    first = lines[j][it.indent + 2:]          # text after "- "
    rows = [(j, " " * key_indent + first)] + [(k, lines[k]) for k in range(j + 1, it.end)]
    for k, text in rows:
        km = _KEY.match(text)
        if not km or len(km.group(1)) != key_indent:
            continue
        key, rest = km.group(2), km.group(3).strip()
        if key == "expr":
            it.expr_line = k
            if rest in ("|", "|-", "|+"):
                end = k + 1
                last = k + 1
                while end < it.end:
                    s = lines[end]
                    if s.strip() and _indent(s) <= key_indent:
                        break
                    if s.strip():
                        last = end + 1
                    end += 1
                it.expr_end = last
                it.expr_text = "\n".join(lines[k + 1:last])
            elif rest and rest[0] not in "\"'>&*!":
                it.expr_end = k + 1
                it.expr_text = rest
            else:
                raise ShapeError(f"{it.name}: expr written as {rest[:2]!r}; use a plain or '|' scalar")
        elif key == "labels":
            for k2 in range(k + 1, it.end):
                s = lines[k2]
                if s.strip() and _indent(s) <= key_indent:
                    break
                lm = _KEY.match(s)
                if lm and lm.group(2) == MAINTENANCE_LABEL:
                    it.label = _unquote(lm.group(3).split(" #")[0])
                    it.label_line = k2


def render_expr(lines: list[str], it: Item, new_expr: str) -> list[str]:
    pad = " " * (it.indent + 2)
    body = [pad + "  " + ln if ln else "" for ln in new_expr.split("\n")]
    return lines[:it.expr_line] + [pad + "expr: |"] + body + lines[it.expr_end:]


# ── per-file work ────────────────────────────────────────────────────────────

def sync_text(text: str, path: str) -> tuple[str, list[str]]:
    """Return (*text* with derived exprs rewritten, problems needing a human)."""
    lines = text.split("\n")
    problems: list[str] = []
    # Rewrite bottom-up so earlier line numbers stay valid.
    for it in reversed(parse_items(lines)):
        if it.expr_text is None:
            problems.append(f"{path}: {it.kind} {it.name}: no expr found")
            continue
        current = _clean(it.expr_text)
        if it.kind == "record":
            mute = False
        elif it.label is None:
            problems.append(f"{path}: alert {it.name}: missing label "
                            f"`{MAINTENANCE_LABEL}: {MUTE}|{PAGE}` — decide whether "
                            f"_state_maintenance mutes it")
            continue
        elif it.label not in VALUES:
            problems.append(f"{path}: alert {it.name}: {MAINTENANCE_LABEL}={it.label!r}, "
                            f"want one of {VALUES}")
            continue
        else:
            mute = it.label == MUTE
        try:
            wanted = derive_expr(it.expr_text, mute)
        except ShapeError as exc:
            problems.append(f"{path}: {it.kind} {it.name}: {exc}")
            continue
        if wanted != current:
            lines = render_expr(lines, it, wanted)
    return "\n".join(lines), problems


def _references(expr: str) -> set[str]:
    return set(_TOKEN.findall(_mask(expr)))


def semantic_problems(rule_files: list) -> list[str]:
    """Check 2 of the module docstring, on the files as Prometheus reads them."""
    records: dict[str, list[str]] = {}
    alerts: list[tuple[str, dict]] = []
    for rf in rule_files:
        for doc in rf.documents:
            for g in (doc or {}).get("groups") or []:
                for r in g.get("rules") or []:
                    if "record" in r:
                        records.setdefault(r["record"], []).append(r.get("expr", ""))
                    elif "alert" in r:
                        alerts.append((rf.path, r))
    problems: list[str] = []

    def count(expr: str, seen: frozenset[str]) -> int:
        n = clause_count(expr)
        for tok in _references(expr):
            if tok in records and tok not in seen:
                n += sum(count(e, seen | {tok}) for e in records[tok])
        return n

    for path, r in alerts:
        name = r["alert"]
        value = (r.get("labels") or {}).get(MAINTENANCE_LABEL)
        if value not in VALUES:
            problems.append(f"{path}: alert {name}: {MAINTENANCE_LABEL}={value!r}, want one of {VALUES}")
            continue
        try:
            n = count(r.get("expr", ""), frozenset())
        except ShapeError as exc:
            problems.append(f"{path}: alert {name}: {exc}")
            continue
        want = 1 if value == MUTE else 0
        if n != want:
            problems.append(f"{path}: alert {name}: {MAINTENANCE_LABEL}={value} but its expr and the "
                            f"recording rules it reads hold {n} maintenance clause(s), want {want}")
    return problems


def _pack(path: Path) -> str | None:
    return _lib_consumer_read.RULE_PACKS.member(path)


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Write the maintenance clause of tenant rule-pack alerts from their "
                    "`maintenance: mute|page` label (#3309).")
    parser.add_argument("--check", action="store_true",
                        help="verify only; exit 1 when a pack is stale or an alert violates the contract")
    parser.add_argument("--rule-packs-dir", default=None,
                        help="rule packs directory (default: the repo's rule-packs/)")
    args = parser.parse_args(argv)

    if args.rule_packs_dir:
        rp_dir = Path(args.rule_packs_dir)
    else:
        rp_dir = require_project_root(_THIS_DIR) / _lib_consumer_read.RULE_PACKS.directory
    try:
        packs = _lib_consumer_read.tenant_rule_pack_files(rp_dir)
    except _lib_consumer_read.ConsumerReadError as exc:
        print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not packs:
        print(f"ERROR: no tenant rule packs under {rp_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    problems: list[str] = []
    stale: list[Path] = []
    for p in packs:
        if _pack(p) == CUSTOM_ALERTS_PACK:
            continue  # the compiler's output: checked below, never written
        text = p.read_text(encoding="utf-8")
        try:
            new, probs = sync_text(text, str(p))
        except ShapeError as exc:
            print(f"ERROR: {p}: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        problems += probs
        if new != text:
            stale.append(p)
            if not args.check:
                p.write_text(new, encoding="utf-8", newline="\n")

    if args.check or not stale:
        try:
            read = _lib_consumer_read.prometheus_rule_files(packs)
        except _lib_consumer_read.ConsumerReadError as exc:
            print(f"ERROR: {_lib_consumer_read.error_text(exc)}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        problems += semantic_problems(read)

    for msg in problems:
        print(f"ERROR: {msg}", file=sys.stderr)
    if args.check and stale:
        for p in stale:
            print(f"ERROR: {p}: maintenance clauses are stale — run "
                  f"`python3 scripts/tools/dx/generate_rulepack_derived.py`", file=sys.stderr)
    if problems or (args.check and stale):
        return EXIT_VIOLATION
    if stale:
        print(f"wrote {len(stale)} rule pack(s): {', '.join(p.name for p in stale)}")
    else:
        print(f"OK — {len(packs)} tenant rule pack(s): every alert's maintenance clause follows its label")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
