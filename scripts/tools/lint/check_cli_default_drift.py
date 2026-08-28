#!/usr/bin/env python3
"""check_cli_default_drift.py — cli-reference 的「預設值」欄 vs argparse 的實際 default。

#1556 的第二個實例：`validate --tolerance` 文件寫預設 `5`、程式是 `0.001`，而
比較式是 `abs(old-new) <= tolerance * max(...)` ⇒ 照文件填 `5` 等於 500% 容差，
每一對都判 match，再配上 `--auto-detect-convergence` 就寫出一份宣稱 ready 的
`cutover-readiness.json`。旗標存在、argparse 收得下、rc=0——旗標存在性的檢查看
不到這一類，所以需要這一支。

⛔ 為什麼是「比對」而不是「生成」（實測，不是偏好）
--------------------------------------------------------------------
生成式（把表格欄位從 argparse 產出）是這類問題的業界標準解，而**這個 repo 目前
不符合它的前提**：這兩欄承載了 argparse 裡沒有的資訊。對 f3026d5f 量過：

    第 1 欄（旗標＋metavar）  435 列：96 列生成會變好、162 列打平、
                              176 列會變差（`<SEC>`→`DURATION`、`<LIST>`→`TENANTS`、
                              `<PATH>`→`CONFIG_DIR`——單位與格式在生成後消失）
    第 3 欄（預設值）        392 列：174 列打平、157 列會變差
                              （`（自動）`／`stdout`／`（全部）` → `-`）

也就是天真的生成會為了修約 61 格而弄壞 333 格。要走生成式必須先讓 argparse 握有
那些資訊（`metavar=` 遷移約 88 處，且會改變出貨的 `--help`）——那是一個獨立決策，
不是這支工具的前提。

⚠️ 這支工具**只評分兩側都是同型字面值的列**，其餘一律不評分並計數揭露。不評分的
那些不是「檢查過沒問題」，是「這個機制看不到」。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lint_helpers import parse_command_map  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = (REPO_ROOT / "docs" / "cli-reference.md",
        REPO_ROOT / "docs" / "cli-reference.en.md")
_SEARCH_DIRS = ("scripts/tools/ops", "scripts/tools/dx", "scripts/tools")

# ⛔ Defaults that read the environment. Introspecting with these set makes the
# captured default a property of the machine, not of the CLI — measured: with
# PROMETHEUS_URL exported, 14 flags across 12 subcommands change their default,
# and this check would go red with no doc change at all. The scrub is the
# difference between a gate and a coin flip.
_SCRUBBED_ENV = ("PROMETHEUS_URL", "TENANT_API_URL", "DA_GOVERN_GROUPS",
                 "DA_AUTH_TOKEN_FILE", "DA_LANG", "LC_ALL", "LANG", "LANGUAGE")

# Header shapes that carry a default column, mapped to that column's index.
# ⛔ PINNED, and an unrecognised header is an ERROR rather than a skip: the
# columns are not in a fixed order (`| Flag | 預設 | 說明 |` puts the default in
# the middle), so positional parsing silently reads the wrong cell, and a
# renamed header would silently remove the table from the scan surface.
_HEADER_DEFAULT_COL: dict[tuple[str, ...], int] = {
    ("選項", "說明", "預設值"): 2,
    ("參數", "說明", "預設值"): 2,
    ("參數", "說明", "預設"): 2,
    ("參數", "用途", "預設"): 2,
    ("Flag", "預設", "說明"): 1,
    ("Flag", "Default", "說明"): 1,
    ("Option", "Description", "Default"): 2,
    ("Parameter", "Description", "Default"): 2,
    ("Argument", "Description", "Default"): 2,
    ("Parameter", "Purpose", "Default"): 2,
    ("Flag", "Default", "Description"): 1,
    ("Flag", "Default", "Purpose"): 1,
}
# Header shapes that legitimately carry NO default column.
_HEADER_NO_DEFAULT: set[tuple[str, ...]] = {
    ("參數", "說明"), ("參數", "說明", "可選值"), ("參數", "說明", "範例"),
    ("Parameter", "Description"), ("Parameter", "Description", "Values"),
    ("Parameter", "Description", "Example"),
    ("代碼", "說明"), ("Code", "Description"), ("Code", "意義"),
    ("Code", "含義"), ("Code", "Meaning"),
    ("Exit Code", "含義", "CI 行為"),
}

_HEADING = re.compile(r"^####\s+(\S+)")
_FLAG_CELL = re.compile(r"^`?(--?[A-Za-z][\w-]*)")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
# ⛔ ASCII, not `\w`. Python's `\w` is Unicode-aware, so `全部` / `無` /
# `內建預設` / `文字模式` all matched and were treated as string LITERALS to
# compare against argparse. They are prose. Measured before this narrowing: a
# cell reading `全部` against `default="all"` produced a finding — a false red
# one edit away, and the parametrised prose test happened to use the
# full-width-parenthesised form `（全部）`, which never matched, so the suite
# could not see it. Real defaults in this repo are ASCII identifiers, numbers,
# paths, URLs or durations.
_PLAIN = re.compile(r"^[A-Za-z0-9_./:@=-]+$")


class Finding(NamedTuple):
    doc: str
    line: int
    command: str
    flag: str
    documented: str
    actual: str


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


def _resolve(script: str) -> Path | None:
    for base in _SEARCH_DIRS:
        candidate = REPO_ROOT / base / script
        if candidate.is_file():
            return candidate
    hits = sorted((REPO_ROOT / "scripts").rglob(script))
    return hits[0] if hits else None


def introspect_defaults() -> tuple[dict[str, dict[str, Any]], list[str],
                                   list[str], list[str]]:
    """subcommand -> {opt: default}, plus unscoreable, BLIND and scan-surface
    faults.

    ⛔ Four channels, because they read differently to an operator: `blind`
    entries are command names ("could not load X"), while `faults` are whole
    sentences about the scan surface. An earlier revision put the second into
    the first and the report read "could not load 51 commands in COMMAND_MAP
    but only 47 accounted for … — this check saw NOTHING for that command".

    The third return value is the load-bearing one: ``unscoreable`` are commands
    this check legitimately cannot score (no argparse, or the tool exits first),
    while ``blind`` are commands it FAILED TO LOAD. The two must not share a
    channel — see the except clauses below.

    Captures the real ``ArgumentParser`` rather than parsing ``--help`` text:
    argparse only prints a default when the argument also carries help text
    (CPython gh-95889), so a help-text reader would report "no default" for
    arguments that have one — the failure would look exactly like a doc that
    correctly omits it.
    """
    saved_env = {k: os.environ.pop(k, None) for k in _SCRUBBED_ENV}
    real_parse = argparse.ArgumentParser.parse_args
    real_parse_known = argparse.ArgumentParser.parse_known_args

    def _capture(self: argparse.ArgumentParser, *_a: Any, **_k: Any) -> Any:
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = _capture  # type: ignore[method-assign]
    argparse.ArgumentParser.parse_known_args = _capture  # type: ignore[method-assign]
    captured: dict[str, dict[str, Any]] = {}
    unscoreable: list[str] = []
    blind: list[str] = []
    command_map = parse_command_map()
    try:
        for command, script in sorted(command_map.items()):
            path = _resolve(script)
            if path is None:
                unscoreable.append(f"{command} (script {script} not found)")
                continue
            saved_argv, saved_path = sys.argv[:], sys.path[:]
            sys.argv = [str(path)]
            sys.path.insert(0, str(path.parent))
            sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    runpy.run_path(str(path), run_name="__main__")
                unscoreable.append(f"{command} (no argparse parser reached)")
            except _Captured as hit:
                captured[command] = {
                    opt: action.default
                    for action in hit.parser._actions
                    for opt in action.option_strings
                    if action.dest != "help"
                }
            except SystemExit:
                # A repo fact, not an apparatus failure: three tools
                # (batch-pr / guard / parser) exit before argparse is reached.
                # Measured; they stay a soft disclosure.
                unscoreable.append(f"{command} (SystemExit before argparse)")
            except BaseException as exc:  # noqa: BLE001 - report, never swallow
                # ⛔ BLIND, not merely unscoreable. Anything else here means the
                # apparatus could not LOAD the tool — an ImportError in a
                # slimmer environment being the case that matters, because this
                # check's own CI leg installs a subset of the tools' deps.
                # Without this split every command failing to import produced
                # `scored 0 rows, 0 matched, 0 drifted` and then
                # "✅ every comparable default matches" at exit 0 — measured by
                # calling scan() with an empty defaults map. A check that
                # cannot see anything must not report that it saw nothing wrong.
                blind.append(f"{command} ({type(exc).__name__}: {exc})")
            finally:
                sys.argv, sys.path = saved_argv, saved_path
    finally:
        argparse.ArgumentParser.parse_args = real_parse  # type: ignore[method-assign]
        argparse.ArgumentParser.parse_known_args = real_parse_known  # type: ignore[method-assign]
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value
    # ⛔ Two invariants, both DERIVED rather than pinned to a number. Blind
    # review reached the same green in three different ways that no floor in
    # the test suite could see:
    #   * `parse_command_map()` returning {} (it does that silently when it
    #     cannot find COMMAND_MAP) → every row lands in `no_parser`, rc=0,
    #     "✅ every comparable default matches";
    #   * one `skip` line in this loop removed 8 subcommands — 44% of the
    #     comparable rows — and a planted drift went invisible while all 24
    #     tests stayed green, because the only floor (`scored >= 100`) sits in
    #     pytest with a 47% cushion under a 188-row baseline.
    # A count cannot see either. The accounting identity can: every command in
    # the map must leave through exactly one of the three doors.
    # ⛔ The denominator is re-read, NOT the `command_map` the loop consumed.
    # Blind review moved the skip three lines up — filtering `command_map`
    # BEFORE the loop instead of `continue`-ing inside it — and the identity
    # went blind, because both sides then shrank together. A guard whose
    # denominator is derived from the thing it is checking measures nothing.
    faults = _scan_surface_faults(parse_command_map(), captured,
                                  unscoreable, blind)
    return captured, unscoreable, blind, faults


def _scan_surface_faults(command_map: dict[str, str],
                         captured: dict[str, dict[str, Any]],
                         unscoreable: list[str],
                         blind: list[str]) -> list[str]:
    """Injectable so the invariant can be tested without editing the loop.

    ⛔ It cannot be provoked from outside: the loop routes every command to one
    of the three doors, so a violation requires a SOURCE EDIT. That is exactly
    what it guards — blind review added one `skip` line and removed 8
    subcommands (44% of the comparable rows) with every test still green.
    """
    if not command_map:
        return ["COMMAND_MAP parsed to zero commands — there was nothing to "
                "introspect, so a clean result here means nothing"]
    accounted = len(captured) + len(unscoreable) + len(blind)
    if accounted != len(command_map):
        return [f"{len(command_map)} commands in COMMAND_MAP but only "
                f"{accounted} accounted for (captured {len(captured)}, "
                f"unscoreable {len(unscoreable)}, blind {len(blind)}) — "
                f"something removed commands from the scan surface"]
    return []


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _doc_literal(cell: str) -> tuple[str, Any] | None:
    """Return (kind, value) when the cell is a literal, else None.

    ⛔ Everything else is prose describing what happens when the flag is
    omitted (`（全部）`, `stdout`, `(interactive prompt)`). argparse has no slot
    for that, so it is neither comparable nor generatable — it is left alone
    and counted, not silently treated as checked.
    """
    text = cell.strip()
    if text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        text = text[1:-1].strip()
    elif "`" in text:
        return None
    low = text.lower()
    if low in ("true", "false"):
        return ("bool", low == "true")
    if _NUMBER.match(text):
        return ("num", float(text))
    if _PLAIN.match(text) and text not in ("-", "stdout", "empty", "all", "none"):
        return ("str", text)
    return None


def _actual_literal(value: Any) -> tuple[str, Any] | None:
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, os.PathLike):
        return ("str", os.fspath(value))
    return None


def _same(kind: str, documented: Any, actual: Any) -> bool:
    if kind == "str":
        # `./conf.d` and `conf.d` name the same directory; a leading `./` or a
        # trailing `/` is presentation, not a different default.
        return (str(documented).rstrip("/").removeprefix("./")
                == str(actual).rstrip("/").removeprefix("./"))
    return documented == actual


def scan(docs: tuple[Path, ...] = DOCS,
         defaults: dict[str, dict[str, Any]] | None = None,
         ) -> tuple[list[Finding], dict[str, int], list[str],
                    dict[tuple[str, ...], int]]:
    """Compare each doc's default column against the real argparse defaults.

    ``docs`` and ``defaults`` are injectable so the guard's own tests can plant
    a known defect and require it to be found — a bare "the repo is clean"
    assertion is satisfied by any change that scans less, which is how this
    class of check goes quietly blind.
    """
    unscoreable: list[str] = []
    blind: list[str] = []
    faults: list[str] = []
    if defaults is None:
        defaults, unscoreable, blind, faults = introspect_defaults()
    findings: list[Finding] = []
    stats = {"scored": 0, "matched": 0, "doc_prose": 0,
             "actual_not_literal": 0, "flag_undeclared": 0, "no_parser": 0}
    # ⛔ blind and faults go into `errors`, NOT into the `unscoreable:`-prefixed
    # notes, because main() reads that prefix as "soft disclosure" and exits 0.
    errors: list[str] = [
        f"could not load {b} — this check saw NOTHING for that command"
        for b in blind] + list(faults)
    # ⛔ Per-header scored counts. A single global floor cannot see one
    # header shape being read from the wrong column: those rows silently
    # become "prose, not scored" and the total stays above any round number.
    by_header: dict[tuple[str, ...], int] = {}
    by_doc: dict[str, int] = {}
    for doc in docs:
        try:
            rel = doc.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = doc.as_posix()
        command: str | None = None
        default_col: int | None = None
        last_header: tuple[str, ...] | None = None
        pending: tuple[str, ...] | None = None
        for number, raw in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            heading = _HEADING.match(raw)
            if heading:
                command = heading.group(1).strip("`")
                default_col = None
                last_header = pending = None
                continue
            if not raw.startswith("|"):
                continue
            cells = _cells(raw)
            key = tuple(c.strip("* ") for c in cells)
            if key in _HEADER_DEFAULT_COL:
                default_col = _HEADER_DEFAULT_COL[key]
                last_header = pending = key
                continue
            if key in _HEADER_NO_DEFAULT:
                default_col = None
                last_header = pending = key
                continue
            if set("".join(cells)) <= set("-: "):
                # A markdown separator row: whatever came immediately before it
                # was this table's header, mapped or not. Tracked so the error
                # below can name the header rather than the offending data row.
                last_header = pending
                continue
            pending = key
            if default_col is None:
                # Not inside a table this checker understands. An unknown header
                # whose first cell looks like a flag is a scan-surface hole, so
                # it is reported rather than skipped.
                if _FLAG_CELL.match(cells[0]) and len(cells) >= 3 and command in defaults:
                    errors.append(
                        f"{rel}:{number}: flag row under an unrecognised table "
                        f"header {last_header!r} — add that header to "
                        f"_HEADER_DEFAULT_COL or _HEADER_NO_DEFAULT. Leaving it "
                        f"unmapped removes the table from this check with no "
                        f"red anywhere.")
                continue
            if command is None or command not in defaults:
                if command is not None:
                    stats["no_parser"] += 1
                continue
            flag_match = _FLAG_CELL.match(cells[0])
            if not flag_match or default_col >= len(cells):
                continue
            flag = flag_match.group(1)
            if flag not in defaults[command]:
                stats["flag_undeclared"] += 1
                continue
            documented = _doc_literal(cells[default_col])
            if documented is None:
                stats["doc_prose"] += 1
                continue
            actual = _actual_literal(defaults[command][flag])
            if actual is None or actual[0] != documented[0]:
                stats["actual_not_literal"] += 1
                continue
            stats["scored"] += 1
            by_doc[rel] = by_doc.get(rel, 0) + 1
            if last_header is not None:
                by_header[last_header] = by_header.get(last_header, 0) + 1
            if _same(documented[0], documented[1], actual[1]):
                stats["matched"] += 1
            else:
                findings.append(Finding(
                    rel, number, command, flag,
                    cells[default_col], repr(defaults[command][flag])))
    # ⛔ Third invariant: a run that compared NOTHING is not a passing run.
    # Blind review deleted every table row from both cli-reference files and
    # got rc=0 with "✅ every comparable default matches" — the check has no
    # other way to notice that its own input vanished.
    if not stats["scored"]:
        errors.append(
            "0 comparable rows — every default cell was skipped, so this "
            "check compared nothing and its silence carries no information")
    # ⛔ PER DOCUMENT, not just in total. Blind review deleted every table row
    # from cli-reference.en.md alone: `scored` fell 192 → 99 and the run still
    # exited 0 — one row above the `scored >= 100` floor that lives in pytest.
    # Half the surface can vanish while the global count stays respectable.
    for doc in docs:
        try:
            rel = doc.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = doc.as_posix()
        if not by_doc.get(rel):
            errors.append(
                f"{rel} contributed 0 comparable rows — this check read the "
                f"file and compared nothing in it, which is indistinguishable "
                f"from that file being correct")
    return (findings, stats,
            errors + [f"unscoreable: {u}" for u in unscoreable], by_header)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="cli-reference default column vs argparse defaults (#1556)")
    parser.add_argument("--ci", action="store_true",
                        help="Exit non-zero when drift is found")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output on stdout")
    args = parser.parse_args()

    for doc in DOCS:
        if not doc.is_file():
            # ⛔ Under --json the consumer is a machine: emitting nothing on
            # stdout hands it a parse error instead of a diagnosis. Measured:
            # deleting cli-reference.en.md gave rc=2 with an empty stdout.
            if args.json:
                import json
                json.dump({"findings": [], "stats": {}, "notes":
                           [f"missing {doc}"]}, sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
            print(f"ERROR: missing {doc}", file=sys.stderr)
            return EXIT_CALLER_ERROR

    findings, stats, notes, _by_header = scan()
    hard_errors = [n for n in notes if not n.startswith("unscoreable:")]

    if args.json:
        import json
        json.dump({"findings": [f._asdict() for f in findings],
                   "stats": stats, "notes": notes},
                  sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        for note in hard_errors:
            print(f"[ERROR] {note}", file=sys.stderr)
        for f in findings:
            print(f"[DRIFT] {f.doc}:{f.line}  {f.command} {f.flag}")
            print(f"          documented: {f.documented}")
            print(f"          argparse  : {f.actual}")
        print()
        print(f"scored {stats['scored']} rows, {stats['matched']} matched, "
              f"{len(findings)} drifted")
        print(f"NOT scored: {stats['doc_prose']} prose default cells, "
              f"{stats['actual_not_literal']} non-literal argparse defaults, "
              f"{stats['flag_undeclared']} rows whose flag the CLI does not "
              f"declare (that class belongs to #1513, not here), "
              f"{stats['no_parser']} rows under a subcommand with no argparse "
              f"parser")
        print("⚠️ NOT scored is a disclosure, not coverage: this check cannot "
              "see those rows.")
        if not findings and not hard_errors:
            print("✅ every comparable default matches")

    if hard_errors:
        return EXIT_VIOLATION if args.ci else EXIT_OK
    if findings and args.ci:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
