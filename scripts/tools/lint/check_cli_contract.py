#!/usr/bin/env python3
"""check_cli_contract.py — 文件裡教人抄的 da-tools 命令 ↔ 活的 argparse 契約（#1379 / TRK-370）。

判定（每條都從兩個獨立來源推導後比對，不列舉壞拼法）：

  V0  ``da-tools <x>`` 的 ``x`` 不是 ``COMMAND_MAP`` 的鍵
  V1  旗標不在該 subcommand 的 parser ``option_strings``（含二層 ``add_subparsers`` 的 choices）
  V2  旗標是恰一個長旗標的前綴——argparse ``allow_abbrev`` 會收下並綁到別的旗標、rc 0；一律違規
  V3  cli-reference 選項表第一欄的旗標不在 parser
  V4  script 以 AST 可達的非零結束碼不在 cli-reference 該命令節的結束碼表（只判「可達但未列」）

契約來源：``parse_command_map`` 給 subcommand→script，每支 script 用 runpy 跑到 ``parse_args``
被攔下為止拿到真的 ``ArgumentParser``；``--prometheus`` 從 entrypoint 的 ``PROMETHEUS_COMMANDS`` 讀。
載體：fenced block 的邏輯行（``diff`` fence 只看 ``+`` 行）、fence 之外的 inline code span、manifest
的 ``command:``／``args:`` 清單、shell 真的會執行的 ``sh -c "…"`` 字串，以及 cli-reference 的選項表
與結束碼表；裸 ``da-tools`` 只在命令位置才是主語。

帳本 ``docs/internal/cli-contract-baseline.yaml`` 每列 (file, command, verdict, token, count, ticket)，
比對是集合相等——少於 count 是 stale 硬錯、多出來的是新 finding、ticket 必須是 ``#NNNN``。
逃生門 ``datools-cmd-ignore: <理由>``（fence 內 shell 註解、散文行尾或表格末格的 HTML 註解）：
理由空是硬錯，同一 finding 同時被帳本與 ignore 豁免是硬錯。

結束碼：閘門自己跑不完回 2；有 finding 或內容面硬錯在 ``--ci`` 下回 1。
輸出末尾的 NOT scored 是揭露不是涵蓋：列在那裡的東西這支工具看不到。

分工：無 parser 的 Go wrapper（``guard``／``parser``／``batch-pr``）只揭露不判，其子命令合法性歸
``check_doc_datools_cmds``；表頭 pin、環境變數清洗與掃描面恆等式直接 import
``check_cli_default_drift``，不抄第二份。
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import runpy
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterator, NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lint_helpers import ENTRYPOINT_PATH, parse_command_map  # noqa: E402
# Imported, not copied: a second transcription of these pins is a second
# thing that can drift.
from check_cli_default_drift import (  # noqa: E402
    _HEADER_DEFAULT_COL, _HEADER_NO_DEFAULT, _SCRUBBED_ENV, _resolve,
    _scan_surface_faults,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_REFERENCE_DOCS = (REPO_ROOT / "docs" / "cli-reference.md",
                      REPO_ROOT / "docs" / "cli-reference.en.md")
BASELINE_PATH = REPO_ROOT / "docs" / "internal" / "cli-contract-baseline.yaml"
# Customer landing pages outside docs/, enumerated by file (same tuple as
# check_doc_datools_cmds); a missing one is a hard error, never a silent shrink.
EXTRA_DOC_FILES = (
    "components/da-tools/README.md",
    "components/da-tools/app/QUICKSTART.md",
    "try-local/README.md",
)
INLINE_IGNORE = "datools-cmd-ignore"
VERDICTS = ("V0", "V1", "V2", "V3", "V4")
_TICKET_RE = re.compile(r"^#\d+$")

# Tokens the dispatcher itself answers before any script runs.
_ENTRYPOINT_GLOBAL = frozenset({"-h", "--help", "help", "--version"})
# Flags argparse itself provides on every parser.
_PARSER_GLOBAL = frozenset({"-h", "--help"})
# Words after which the NEXT word is a command: the container runtimes' `run`
# / `exec`, POSIX `--` (end of options) and the coreutils / POSIX wrappers
# that exec their operand.
_COMMAND_WRAPPERS = frozenset({"run", "exec", "--", "sudo", "env", "time",
                               "nohup", "xargs"})
# POSIX shell reserved words after which a command begins (`if da-tools …;
# then`, `if ! da-tools …`, `{ da-tools …; }`).
_RESERVED_WORDS = frozenset({"if", "!", "{", "then", "else", "elif", "do",
                             "while", "until"})
_DASH_C = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")   # `-c`, `-lc`, `-ec`, `-cl`
# Container runtimes whose `run` takes an image operand — used only to count
# a `run` we could not resolve to a da-tools image.
_CONTAINER_RUNTIMES = frozenset({"docker", "podman", "nerdctl", "kubectl"})
# CI/manifest keys whose value IS a command line (GitHub Actions / GitLab CI /
# compose / k8s schemas), so `run: da-tools …` starts at da-tools.
_COMMAND_KEYS = frozenset({"run:", "script:", "command:", "entrypoint:", "cmd:",
                           "before_script:", "after_script:"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})

_HEADING = re.compile(r"^####\s+`?([A-Za-z][\w-]*)`?\s*$")
_H4 = re.compile(r"^####\s")
_SECTION_END = re.compile(r"^#{1,3}\s")
_FLAG_IN_CELL = re.compile(r"(?<![\w./-])(--?[A-Za-z][\w-]*)")
# A first cell that IS a flag spec: an optional single code span whose text
# starts with a flag. `\`--json\` 的 \`coverage[]\`` (two spans, prose between)
# is a description that mentions a flag, not a flag row.
_FLAG_ROW = re.compile(r"^`?-{1,2}[A-Za-z][\w-]*(?:[^`]*`)?\s*$")
# `\`1\``, `1`, or several codes in one cell: `\`1\` / \`2\``.
_CODE_CELL = re.compile(r"^`?\d+`?(?:\s*/\s*`?\d+`?)*$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
# A marker is the token at the START of the comment, then a colon, then the
# reason: `# see datools-cmd-ignore in docs` is prose about the marker and
# `datools-cmd-ignored` is another word. A marker without the colon has no
# reason and is the same hard error as an empty one.
_ROW_IGNORE = re.compile(r"<!--\s*" + INLINE_IGNORE + r"\b\s*(:)?\s*(.*?)\s*-->")
_FENCE_IGNORE = re.compile(r"^\s*" + INLINE_IGNORE + r"\b\s*(:)?\s*(.*)$")
_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})\s*([^`\s]*)")
_SPAN = re.compile(r"`([^`\n]+)`")
_IMAGE_REF = re.compile(r"^(?:[\w.-]+/)*da-tools(?:[:@][^\s\\]+)?$")
_PY_INTERPRETERS = frozenset({"python", "python3", "py"})
_REDIRECT = re.compile(r"^(\d*>|<)")
# argparse's own `_negative_number_matcher`: `-1` and `-.5` are values, `-1d`
# is an option string (rc=2 when undeclared).
_NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")
_PLACEHOLDER_CHARS = "<>${}[]…"
_SHELL_PUNCT = "();|&"
_PUNCT_UNIT = re.compile(r"\(|\)|\|\||&&|;;|;|\||&")
_SUBSTITUTION = "$(...)"
# Exit-code table headers = the pinned no-default headers whose first column
# names a code. Derived from the shared pin, not transcribed.
_EXIT_HEADERS: frozenset[tuple[str, ...]] = frozenset(
    h for h in _HEADER_NO_DEFAULT if h[0] in ("代碼", "Code", "Exit Code"))
_OPTION_HEADERS: frozenset[tuple[str, ...]] = frozenset(
    set(_HEADER_DEFAULT_COL) | (set(_HEADER_NO_DEFAULT) - _EXIT_HEADERS))
_EXIT_NAMES = {"EXIT_OK": 0, "EXIT_VIOLATION": 1, "EXIT_CALLER_ERROR": 2}


class Finding(NamedTuple):
    verdict: str
    file: str
    line: int
    command: str
    token: str
    message: str
    ignored: str | None = None  # the ignore reason when suppressed inline

    def key(self) -> tuple[str, str, str, str]:
        return (self.file, self.command, self.verdict, self.token)


class ParserModel(NamedTuple):
    options: frozenset[str]           # every option string, all actions
    arity: dict[str, Any]             # option string -> 0 | 1 | int | "var" | "?"
    positionals: tuple[tuple[str, Any, tuple[str, ...] | None], ...]
    subparsers: dict[str, "ParserModel"] | None
    subparser_slot: int | None        # index among positional tokens, or None


class _Captured(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


# ---------------------------------------------------------------------------
# Contract side: parsers, injected flags, exit codes
# ---------------------------------------------------------------------------
def _arity(action: argparse.Action) -> Any:
    n = action.nargs
    if n == 0:
        return 0
    if n is None:
        return 1
    if isinstance(n, int):
        return n
    if n == "?":
        return "?"
    return "var"


def _model(parser: argparse.ArgumentParser) -> ParserModel:
    options: set[str] = set()
    arity: dict[str, Any] = {}
    positionals: list[tuple[str, Any, tuple[str, ...] | None]] = []
    subparsers: dict[str, ParserModel] | None = None
    slot: int | None = None
    # Positional TOKENS consumed before the subparsers action: a positional
    # with nargs=2 is two tokens, and one with a variable nargs makes the
    # slot undecidable (None) — the walk then stops judging at that point.
    tokens_before = 0
    decidable = True
    for action in parser._actions:
        if action.option_strings:
            for opt in action.option_strings:
                options.add(opt)
                arity[opt] = _arity(action)
            continue
        if isinstance(action, argparse._SubParsersAction):
            subparsers = {name: _model(sub) for name, sub in action.choices.items()}
            slot = tokens_before if decidable else None
            continue
        choices = tuple(str(c) for c in action.choices) if action.choices else None
        positionals.append((action.dest, _arity(action), choices))
        if isinstance(_arity(action), int):
            tokens_before += _arity(action)
        else:
            decidable = False
    return ParserModel(frozenset(options), arity, tuple(positionals),
                       subparsers, slot)


def introspect_parsers() -> tuple[dict[str, ParserModel], list[str], list[str],
                                  list[str]]:
    """subcommand -> ParserModel, plus unscoreable / blind / scan-surface faults.

    Same four channels and the same routing rules as
    ``check_cli_default_drift.introspect_defaults`` (blind ≠ unscoreable; the
    accounting identity is re-read from COMMAND_MAP, not from the loop's copy).
    """
    saved_env = {k: os.environ.pop(k, None) for k in _SCRUBBED_ENV}
    real_parse = argparse.ArgumentParser.parse_args
    real_parse_known = argparse.ArgumentParser.parse_known_args

    def _capture(self: argparse.ArgumentParser, *_a: Any, **_k: Any) -> Any:
        raise _Captured(self)

    argparse.ArgumentParser.parse_args = _capture  # type: ignore[method-assign]
    argparse.ArgumentParser.parse_known_args = _capture  # type: ignore[method-assign]
    captured: dict[str, ParserModel] = {}
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
                captured[command] = _model(hit.parser)
            except SystemExit:
                unscoreable.append(f"{command} (SystemExit before argparse)")
            except BaseException as exc:  # noqa: BLE001 - report, never swallow
                blind.append(f"{command} ({type(exc).__name__}: {exc})")
            finally:
                sys.argv, sys.path = saved_argv, saved_path
    finally:
        argparse.ArgumentParser.parse_args = real_parse  # type: ignore[method-assign]
        argparse.ArgumentParser.parse_known_args = real_parse_known  # type: ignore[method-assign]
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value
    faults = _scan_surface_faults(parse_command_map(), captured, unscoreable, blind)
    return captured, unscoreable, blind, faults


def prometheus_commands(entrypoint: Path = ENTRYPOINT_PATH) -> set[str]:
    """Subcommands entrypoint.py injects ``--prometheus`` into, read from its AST."""
    tree = ast.parse(entrypoint.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "PROMETHEUS_COMMANDS"
                   for t in node.targets):
            continue
        if isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            return {e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return set()


class ExitCodes(NamedTuple):
    reachable: dict[int, list[int]]   # code -> source lines
    undecidable: list[int]            # lines whose exit expression is opaque


def _own_returns(fn: ast.AST) -> Iterator[ast.Return]:
    """``return`` statements of *fn* itself — nested defs and lambdas return
    to their own callers, not to ``sys.exit``."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def reachable_exit_codes(source: str) -> ExitCodes:
    """Non-zero exit codes a script can produce, by AST.

    Resolves ``sys.exit(<expr>)`` / ``raise SystemExit(<expr>)`` /
    ``die_caller_error(...)`` (=2), where ``<expr>`` is an ``EXIT_*`` name, an
    int literal, an ``IfExp`` (both arms), a module-level name bound to an int
    literal, or a module-level function whose own ``return`` statements
    resolve by the same rules (``sys.exit(main())``). Anything else is
    undecidable and disclosed with its line. argparse's own rc 2 is not looked
    for here: the caller adds it for every command that HAS a parser.
    """
    tree = ast.parse(source)
    consts = dict(_EXIT_NAMES)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, int) \
                and not isinstance(node.value.value, bool):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value.value
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    reachable: dict[int, list[int]] = {}
    undecidable: list[int] = []

    def _add(code: int, line: int) -> None:
        reachable.setdefault(code, []).append(line)

    def _resolve_expr(expr: ast.AST | None, line: int, depth: int) -> None:
        if expr is None:
            _add(0, line)
        elif isinstance(expr, ast.Constant) and isinstance(expr.value, int) \
                and not isinstance(expr.value, bool):
            _add(expr.value, line)
        elif isinstance(expr, ast.Constant) and expr.value is None:
            _add(0, line)
        elif isinstance(expr, ast.Name) and expr.id in consts:
            _add(consts[expr.id], line)
        elif isinstance(expr, ast.Attribute) and expr.attr in consts:
            _add(consts[expr.attr], line)
        elif isinstance(expr, ast.IfExp):
            _resolve_expr(expr.body, line, depth)
            _resolve_expr(expr.orelse, line, depth)
        elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) \
                and expr.func.id in funcs and depth < 3:
            for ret in _own_returns(funcs[expr.func.id]):
                _resolve_expr(ret.value, ret.lineno, depth + 1)
        else:
            undecidable.append(line)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else None)
            if name == "exit" and isinstance(f, ast.Attribute) \
                    and isinstance(f.value, ast.Name) and f.value.id == "sys":
                _resolve_expr(node.args[0] if node.args else None, node.lineno, 0)
            elif name == "die_caller_error":
                _add(EXIT_CALLER_ERROR, node.lineno)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and isinstance(node.exc.func, ast.Name) \
                and node.exc.func.id == "SystemExit":
            _resolve_expr(node.exc.args[0] if node.exc.args else None,
                          node.lineno, 0)
    return ExitCodes(reachable, sorted(set(undecidable)))


# ---------------------------------------------------------------------------
# Shell-ish text helpers
# ---------------------------------------------------------------------------
def _cells(line: str) -> list[str]:
    r"""Table cells, splitting only on unescaped pipes.

    Not the sibling's ``_cells``, which splits on every ``|`` and cuts a
    flag cell such as ``\`--format md\|json\``` in two. No header carries an
    escaped pipe, so both readers still agree on which table they are in.
    """
    return [c.strip() for c in _UNESCAPED_PIPE.split(line.strip().strip("|"))]


def _unquote_md(line: str) -> str:
    s = line.lstrip()
    return s[2:] if s.startswith("> ") else (s[1:] if s.startswith(">") else line)


class _Fence(NamedTuple):
    char: str
    size: int
    info: str
    block: int          # ordinal of the block in the file: two equal fences differ


def _fence_open(line: str, block: int) -> _Fence | None:
    m = _FENCE_OPEN.match(line.lstrip())
    if not m:
        return None
    return _Fence(m.group(1)[0], len(m.group(1)), m.group(2).lower(), block)


def walk_lines(lines: list[str]) -> Iterator[tuple[int, str, _Fence | None]]:
    """(number, unquoted line, open fence or None) with CommonMark fence rules.

    A fence closes only on a run of the SAME character at least as long as
    the one that opened it, so a ```` fence containing ``` blocks keeps them
    as content — the same command lines pymdownx.superfences renders.
    """
    fence: _Fence | None = None
    blocks = 0
    for number, raw in enumerate(lines, 1):
        line = _unquote_md(raw)
        opened = _fence_open(line, blocks)
        if fence is None:
            if opened:
                fence = opened
                blocks += 1
                continue
            yield number, line, None
            continue
        if opened and opened.char == fence.char and opened.size >= fence.size \
                and not opened.info:
            fence = None
            continue
        yield number, line, fence


def split_shell_comment(line: str) -> tuple[str, str | None]:
    """(code, comment) — ``#`` opens a comment at a word start, never in quotes."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i], line[i + 1:]
    return line, None


def _ignore_reason(m: re.Match[str]) -> str:
    """The reason of a matched marker; empty when the colon or the text is missing."""
    return m.group(2).strip() if m.group(1) else ""


def _is_placeholder(tok: str) -> bool:
    return any(c in tok for c in _PLACEHOLDER_CHARS) or tok == "..."


def _looks_like_flag(tok: str) -> bool:
    return tok.startswith("-") and tok != "-" and tok != "--" \
        and not _NEGATIVE_NUMBER.match(tok)


def tokenize(code: str) -> list[str] | None:
    lex = shlex.shlex(code, posix=True, punctuation_chars=_SHELL_PUNCT)
    lex.whitespace_split = True
    try:
        raw = [t for t in lex if t != "\\"]
        # shlex glues adjacent operators into one token (`);`, `)||`, `))`,
        # `((`); split them back into units so parentheses match one by one.
        units: list[str] = []
        for t in raw:
            units.extend(_PUNCT_UNIT.findall(t) if set(t) <= set(_SHELL_PUNCT) else [t])
        return _collapse_substitutions(units)
    except ValueError:
        return None


def _collapse_substitutions(tokens: list[str]) -> list[str]:
    """``$( … )`` becomes ONE placeholder token, parentheses matched.

    shlex hands `$` `(` … `)` out separately, and `(` is also the subshell
    operator — without this, `--expect $(jq …) --next` is cut at the `(` and
    every flag after it goes unjudged.
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        # `$` may carry a prefix: `$(id -u):$(id -g)` lexes as `$` `(` … `)`
        # `:$` `(` … `)`. `$((1+2))` and `$(dirname $(pwd))` nest: depth
        # counts every unit.
        if tokens[i].endswith("$") and i + 1 < len(tokens) and tokens[i + 1] == "(":
            depth = 0
            j = i + 1
            while j < len(tokens):
                depth += (tokens[j] == "(") - (tokens[j] == ")")
                if depth == 0:
                    break
                j += 1
            if depth != 0:
                raise ValueError("unbalanced $(")     # caller: unparseable
            out.append(tokens[i][:-1] + _SUBSTITUTION)
            i = j + 1
            continue
        out.append(tokens[i])
        i += 1
    return out


def split_commands(tokens: list[str]) -> list[list[str]]:
    """Command segments: split at control operators, redirects dropped.

    Control operators (``|`` ``&&`` ``;`` ``&`` ``(`` ``)``) start a new
    segment — `cmd && da-tools x --bad` has TWO commands and both are judged.
    A heredoc (``<<``) ends the line. A redirection (``> f``, ``>f``,
    ``2>/dev/null``, ``2>&1``, ``< f``) is dropped together with its target and
    the walk CONTINUES: the shell still hands ``--flag`` in ``cmd > out.json
    --flag`` to ``cmd``. ``<tenant>`` is a placeholder, not a redirect.
    """
    segments: list[list[str]] = []
    current: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = tok == "&"       # `2>&1`: `&` dups, the fd follows
            continue
        if tok.startswith("<<"):
            break
        if set(tok) <= set(_SHELL_PUNCT):
            if current:
                segments.append(current)
            current = []
            continue
        if _REDIRECT.match(tok) and not (tok.startswith("<") and ">" in tok):
            skip_next = tok.rstrip("<>0123456789") == ""
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


class Subject(NamedTuple):
    kind: str          # "da-tools" | "image" | "python"
    command: str | None
    args: list[str]
    note: str | None   # disclosure when command is None


def _command_start(segment: list[str]) -> int:
    """Index of the first token that can be a command word.

    Skips a `$` prompt, `VAR=value` assignments and a CI/manifest key
    (`run:`, `script:`) — the tokens that legitimately precede the command
    on the same line.
    """
    i = 0
    while i < len(segment):
        tok = segment[i]
        if tok == "$" or tok == "-" or _ASSIGNMENT.match(tok) \
                or tok in _COMMAND_KEYS:
            i += 1
            continue
        break
    return i


def _in_command_position(segment: list[str], i: int, start: int) -> bool:
    """Is token *i* where a shell would read a command word?

    Either the first word of the segment, or the word after a wrapper that
    execs its operand (`sudo`, `env`, `time`, `xargs -I{}`, `docker compose
    run --rm`, `kubectl exec pod --`). Walking back over the wrapper's own
    `-flags` is what lets `xargs -I{} da-tools` through; `docker build -t
    da-tools .` walks back to `build`, which execs nothing.
    """
    if i == start:
        return True
    j = i - 1
    while j > start and (_looks_like_flag(segment[j]) or _ASSIGNMENT.match(segment[j])):
        j -= 1
    return segment[j] in _COMMAND_WRAPPERS or segment[j] in _RESERVED_WORDS


def find_subject(segment: list[str], command_map: dict[str, str],
                 stats: dict[str, int] | None = None) -> Subject | None:
    """The da-tools argv in one command segment, or None.

    Three forms: the bare binary (in command position only), an image
    reference after `run` (everything BEFORE it is a docker flag), and
    ``python3 …/<script>.py`` reverse-mapped through COMMAND_MAP.
    """
    by_script = {v: k for k, v in command_map.items()}
    start = _command_start(segment)
    for i in range(start, len(segment)):
        tok = segment[i]
        if tok == "da-tools" or (tok.endswith("/da-tools") and tok[0] in "./~$"):
            if _in_command_position(segment, i, start):
                return Subject("da-tools", None, segment[i + 1:], None)
            if stats is not None:
                nxt = segment[i + 1] if i + 1 < len(segment) else ""
                if nxt in command_map or nxt in _ENTRYPOINT_GLOBAL:
                    # `docker exec <ctr> da-tools lint`, `timeout 30 da-tools
                    # …` (a wrapper that eats an operand this check does not
                    # model), `│ da-tools migrate → …` (a box diagram), `[ ] 6.
                    # Sample: da-tools tenant-verify …` (a checklist item): an
                    # invocation outside command position. Disclosed, not judged.
                    stats["cmd_outside_command_position"] += 1
                else:
                    # `docker build -t da-tools .`, `helm install da-tools …`,
                    # `- name: da-tools`: the word names something.
                    stats["cmd_bare_not_command"] += 1
            continue
        if _IMAGE_REF.match(tok):
            # An image reference is a subject only where the image is RUN:
            # `docker pull/push/tag`, `kind load docker-image` and a YAML
            # `image:` line name the same reference and execute nothing.
            # "Something before it is `run`" is the derivable property
            # (docker / podman / nerdctl / `compose run` all spell it that way).
            if "run" not in segment[start:i]:
                continue
            if any(t == "--entrypoint" or t.startswith("--entrypoint=")
                   for t in segment[:i]):
                return Subject("image", None, segment[i + 1:],
                               "entrypoint overridden; argv is not da-tools'")
            return Subject("image", None, segment[i + 1:], None)
        if tok in _PY_INTERPRETERS:
            # interpreter flags (`py -3`, `python3 -X utf8`) sit before the script
            j = i + 1
            while j < len(segment) and segment[j].startswith("-"):
                j += 1 + (segment[j] in ("-X", "-W", "-m", "-c"))
            if j < len(segment) and segment[j].endswith(".py"):
                base = segment[j].rsplit("/", 1)[-1]
                if base in by_script:
                    return Subject("python", by_script[base], segment[j + 1:], None)
                if "scripts/tools/" in segment[j]:
                    return Subject("python", None, segment[j + 1:],
                                   f"script not in COMMAND_MAP: {base}")
    if stats is not None and segment and segment[start:start + 1] \
            and segment[start] in _CONTAINER_RUNTIMES and "run" in segment[start:]:
        # `docker run $IMAGE …`, `kubectl run x --image=… -- …`: something is
        # run and we cannot tell what. Disclosed, not guessed.
        stats["cmd_run_without_image"] += 1
    return None


# ---------------------------------------------------------------------------
# Judging one argv
# ---------------------------------------------------------------------------
class _Resolved(NamedTuple):
    verdict: str | None     # None = accepted; "V2" = unique prefix; "V1" = rejected
    option: str | None      # the option string argparse would bind
    attached: bool          # the value is inside the token (`-oVALUE`, `-vqoX`)


def resolve_flag(name: str, model: ParserModel) -> _Resolved:
    """One flag token against one parser, with argparse's own rules.

    Long: exact, else a UNIQUE prefix (V2), else rejected (unknown or an
    ambiguous prefix — argparse rc 2 either way). Short: exact, else a
    cluster `-vq[o[VALUE]]` where every character is a declared short option,
    all but the last take no value, and the last may carry its value inline.
    """
    if name in model.options or name in _PARSER_GLOBAL:
        return _Resolved(None, name, False)
    if name.startswith("--"):
        hits = sorted(o for o in model.options
                      if o.startswith("--") and o.startswith(name))
        if len(hits) == 1:
            return _Resolved("V2", hits[0], False)
        return _Resolved("V1", None, False)
    for k in range(1, len(name)):
        short = "-" + name[k]
        if short not in model.options:
            return _Resolved("V1", None, False)
        if model.arity.get(short) != 0:
            # this one takes a value: the rest of the token is that value
            return _Resolved(None, short, k + 1 < len(name))
    return _Resolved(None, "-" + name[-1], False)


def judge_argv(args: list[str], command: str, model: ParserModel | None,
               injected: frozenset[str], stats: dict[str, int],
               file: str, line: int) -> list[Finding]:
    """Findings for the tokens after the subcommand of one command."""
    findings: list[Finding] = []
    if model is None:
        stats["cmd_no_parser"] += 1
        return findings
    parser = model
    positional_index = 0
    after_double_dash = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if after_double_dash:
            stats["cmd_positionals"] += 1
            continue
        if tok == "--":
            after_double_dash = True
            continue
        if _looks_like_flag(tok):
            name, eq, _val = tok.partition("=")
            if _is_placeholder(name):
                stats["cmd_placeholder_flags"] += 1
                continue
            if name in injected:
                res = _Resolved(None, name, False)
                arity: Any = 1
            else:
                res = resolve_flag(name, parser)
                arity = parser.arity.get(res.option, 0) if res.option else 0
                if res.attached:
                    arity = 0
            stats["scored"] += 1
            if res.verdict == "V2":
                findings.append(Finding(
                    "V2", file, line, command, name,
                    f"`{name}` is not a flag of `{command}`; argparse accepts it "
                    f"only as an abbreviation of `{res.option}` (rc=0, bound to "
                    f"that flag — #1514). Spell it out as `{res.option}` AND "
                    f"re-check the value and the prose around it: the target "
                    f"flag's meaning (a directory vs a file, a different unit) "
                    f"is what the abbreviation was hiding, so the example and "
                    f"its expected output change with it (#1514)."))
            elif res.verdict == "V1":
                findings.append(Finding(
                    "V1", file, line, command, name,
                    f"`{command}` does not declare `{name}` — copying this line "
                    f"gives `unrecognized arguments`, rc=2. Use a flag the CLI "
                    f"declares (see `da-tools {command} --help`); a deliberately "
                    f"aspirational example needs `# {INLINE_IGNORE}: <why>`."))
                if not eq and i < len(args) and not _looks_like_flag(args[i]):
                    # The word after an undeclared flag is most likely ITS
                    # value; read as a positional it would be a second, noise
                    # finding (`--repo r snapshot` → `r` "not an action").
                    stats["cmd_skipped_values"] += 1
                    i += 1
                continue
            if eq or arity == 0:
                continue
            if arity == "?" or arity == 1 or isinstance(arity, int):
                want = 1 if arity == "?" else arity
                while want > 0 and i < len(args) and not _looks_like_flag(args[i]):
                    i += 1
                    want -= 1
            else:  # "var": greedy until the next flag-looking token
                while i < len(args) and not _looks_like_flag(args[i]):
                    i += 1
            continue
        # a non-flag token: positional, or the sub-subcommand slot
        if parser.subparsers is not None and positional_index == parser.subparser_slot:
            if tok in parser.subparsers:
                parser = parser.subparsers[tok]
                positional_index = 0
                stats["scored"] += 1
                continue
            if _is_placeholder(tok):
                stats["cmd_placeholder_subcommands"] += 1
                return findings
            stats["scored"] += 1
            findings.append(Finding(
                "V1", file, line, command, tok,
                f"`{command}` has no action `{tok}` (choices: "
                f"{', '.join(sorted(parser.subparsers))})."))
            return findings
        positional_index += 1
        stats["cmd_positionals"] += 1
    return findings


def _any_shell_c(segment: list[str], start: int) -> int | None:
    """Index of the first `-c` cluster right after a shell word, wherever it sits."""
    for k in range(start, len(segment) - 1):
        if segment[k].rsplit("/", 1)[-1] in _SHELLS and _DASH_C.match(segment[k + 1]):
            return k + 1
    return None


def _shell_c_index(segment: list[str], start: int) -> int | None:
    """Index of the `-c` cluster (`-c`, `-lc`, `-cl`) of a shell that RUNS, or None.

    Three placements: `sh -c …` with the shell in command position;
    `docker run --entrypoint sh <image> -c …` (the `-c` follows the image);
    and `docker run … <image> sh -c …` under an image that is NOT da-tools.
    A shell after an operand-eating wrapper (`docker exec <ctr> sh -c …`) is
    not one of them: its string is disclosed by judge_tokens, not judged.
    """
    for k in range(start, len(segment) - 1):
        if segment[k].rsplit("/", 1)[-1] in _SHELLS and _DASH_C.match(segment[k + 1]):
            before = segment[start:k]
            if _in_command_position(segment, k, start):
                return k + 1
            if "run" in before and not any(_IMAGE_REF.match(t) for t in before):
                return k + 1
        if _IMAGE_REF.match(segment[k]) and _DASH_C.match(segment[k + 1]):
            before = segment[start:k]
            if any(t in ("--entrypoint",) or t.startswith("--entrypoint=") for t in before):
                shell = next((before[i + 1] for i, t in enumerate(before[:-1])
                              if t == "--entrypoint"), "")
                shell = shell or next((t.split("=", 1)[1] for t in before
                                       if t.startswith("--entrypoint=")), "")
                if shell.rsplit("/", 1)[-1] in _SHELLS:
                    return k + 1
    return None


class _Ctx(NamedTuple):
    command_map: dict[str, str]
    parsers: dict[str, ParserModel]
    injected_for: dict[str, frozenset[str]]
    stats: dict[str, int]


def judge_tokens(tokens: list[str], rel: str, number: int, ctx: _Ctx,
                 carrier: str, depth: int = 0) -> list[Finding]:
    """Every command segment of one tokenised line, judged."""
    findings: list[Finding] = []
    for segment in split_commands(tokens):
        start = _command_start(segment)
        dash_c = _shell_c_index(segment, start)
        if depth == 0 and dash_c is not None:
            # `sh -c "da-tools …"` / `bash -lc '…'`: the quoted string is the
            # command line, the rest is wrapper. Only when the shell really
            # runs: in command position, as `--entrypoint sh`, or as the argv
            # of a `run` under an image that is NOT da-tools. Under a da-tools
            # image with no --entrypoint, `sh` is argv[0] handed to
            # entrypoint.py — `Unknown command 'sh'` — and stays a V0 below
            # (the string's own da-tools is then disclosed, see subject is None).
            inner = tokenize(segment[dash_c + 1]) if dash_c + 1 < len(segment) else None
            if inner is None:
                ctx.stats["cmd_unparseable"] += 1
            else:
                ctx.stats["cmd_sh_c_strings"] += 1
                findings += judge_tokens(inner, rel, number, ctx, carrier, depth + 1)
            continue
        if depth == 0:
            # A shell string that does NOT run as such (`docker exec <ctr> sh
            # -c "da-tools …"`, `sh` as argv[0] of a da-tools image) may still
            # carry a da-tools inside: disclosed, not judged.
            any_c = _any_shell_c(segment, start)
            inner = tokenize(segment[any_c + 1]) \
                if any_c is not None and any_c + 1 < len(segment) else None
            if inner and any(find_subject(seg, ctx.command_map) is not None
                             for seg in split_commands(inner)):
                ctx.stats["cmd_outside_command_position"] += 1
        subject = find_subject(segment, ctx.command_map, ctx.stats)
        if subject is None:
            continue
        ctx.stats[carrier] += 1
        args = subject.args
        command = subject.command
        seg_findings: list[Finding] = []
        if command is None and subject.note:
            ctx.stats["cmd_entrypoint_override" if subject.kind == "image"
                      else "cmd_script_not_in_map"] += 1
            continue
        if command is None:
            if not args:
                continue
            head = args[0]
            if head in _ENTRYPOINT_GLOBAL:
                continue
            if _is_placeholder(head):
                ctx.stats["cmd_placeholder_subcommands"] += 1
                continue
            ctx.stats["scored"] += 1
            command = head
            if head not in ctx.command_map:
                seg_findings.append(Finding(
                    "V0", rel, number, head, head,
                    f"`da-tools {head}` — no such subcommand in COMMAND_MAP "
                    f"(rc=2). Use a real subcommand; a deliberately aspirational "
                    f"example needs `{INLINE_IGNORE}: <why>` (shell comment in a "
                    f"fence, `<!-- … -->` in prose or a table row)."))
            else:
                args = args[1:]
        if not seg_findings:
            seg_findings = judge_argv(
                args, command, ctx.parsers.get(command),
                ctx.injected_for.get(command, frozenset()), ctx.stats, rel, number)
        findings += seg_findings
    return findings


# ---------------------------------------------------------------------------
# Carrier A: fenced commands, inline spans, manifest args
# ---------------------------------------------------------------------------
def _logical_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Fenced-block content as (first line number, joined text) pairs.

    In a ``diff`` fence only the ``+`` lines are commands a reader would copy
    (the ``+`` stripped); ``-`` lines are the OLD text and are not judged.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    current: _Fence | None = None
    for number, line, fence in walk_lines(lines):
        if fence != current:
            if buf:
                out.append((start, " ".join(buf)))
                buf = []
            current = fence
        if fence is None:
            continue
        if fence.info == "diff":
            if not line.startswith("+"):
                continue
            line = line[1:]
        if not buf:
            start = number
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1].rstrip())
            continue
        buf.append(line)
        out.append((start, " ".join(buf)))
        buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def _fence_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    """(first line number, lines) of every fenced block."""
    blocks: list[tuple[int, list[str]]] = []
    buf: list[str] = []
    start = 0
    current: _Fence | None = None
    for number, line, fence in walk_lines(lines):
        if fence != current:
            if buf:
                blocks.append((start, buf))
                buf = []
            current = fence
            start = number
        if fence is not None:
            buf.append(line)
    if buf:
        blocks.append((start, buf))
    return blocks


_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*)$")
_KEY = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z_]+):\s*(.*)$")


def _yaml_list(value: str, following: list[str], indent: int) -> list[str] | None:
    """Items of a YAML list given inline (`[a, "b"]`) or as `- item` lines."""
    value = value.strip()
    if value.startswith("["):
        if not value.endswith("]"):
            return None
        return [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
    if value:
        return None
    items: list[str] = []
    for raw in following:
        m = _LIST_ITEM.match(raw)
        if not m or len(m.group(1)) < indent:      # `- item` may sit at the key's indent
            break
        items.append(m.group(2).strip().strip("\"'"))
    return items


def manifest_argvs(block: list[str], stats: dict[str, int] | None = None
                   ) -> list[tuple[int, list[str]]]:
    """da-tools argvs declared by ``command:``/``args:`` lists.

    Two schemas, told apart by the enclosing key: under k8s ``containers:``
    the ``command:`` REPLACES the image entrypoint (a da-tools image running
    ``["python3", "-m", "http.server"]`` runs no da-tools at all; counted as
    an entrypoint override), so only
    ``command: ["da-tools", …]``, a shell ``-c`` string, or ``args:`` under
    an untouched da-tools image are argv. Under compose ``services:`` the
    ``command:`` is the CMD and the entrypoint stays da-tools unless an
    ``entrypoint:`` key replaces it, so the whole list is argv. Returns
    (offset within block, argv).
    """
    out: list[tuple[int, list[str]]] = []
    image_is_datools = False
    compose = False
    command: list[str] | None = None
    command_at = 0
    key_indent: int | None = None    # indent of the container's own keys

    def _flush() -> None:
        if not command:
            return
        if command[0] == "da-tools" and len(command) > 1:
            # `command: ["da-tools", "widget", …]` with no `args:` at all
            out.append((command_at, command[1:]))
        elif image_is_datools and command[0].rsplit("/", 1)[-1] not in _SHELLS:
            if compose:
                out.append((command_at, list(command)))
            elif stats is not None:
                stats["cmd_entrypoint_override"] += 1

    for idx, raw in enumerate(block):
        m = _KEY.match(raw)
        if not m:
            continue
        indent, key, value = len(m.group(1)), m.group(2), m.group(3)
        if key == "services":
            compose = True
        elif key == "containers":
            compose = False
        if key == "name" and raw.lstrip().startswith("-"):
            # A new container only when the dash sits OUTSIDE the current
            # container's keys: `env:` holds `- name: PROMETHEUS_URL` items
            # deeper than `image:`, and those are not a boundary.
            if key_indent is None or indent < key_indent:
                _flush()
                image_is_datools, command, key_indent = False, None, None
            continue
        if key_indent is None or indent < key_indent:
            key_indent = indent
        if key == "image":
            image_is_datools = bool(_IMAGE_REF.match(value.strip().strip("\"'")))
        elif key == "entrypoint" and compose:
            # compose `entrypoint:` replaces the image's; only `da-tools`
            # itself keeps the `command:` list as da-tools' argv
            ep = _yaml_list(value, block[idx + 1:], indent)
            ep = ep if ep is not None else value.strip().strip("\"'").split()
            if ep and ep[0].rsplit("/", 1)[-1] != "da-tools" and image_is_datools:
                image_is_datools = False
                if stats is not None:
                    stats["cmd_entrypoint_override"] += 1
        elif key == "command":
            command, command_at = _yaml_list(value, block[idx + 1:], indent), idx
            if command and command[0].rsplit("/", 1)[-1] in _SHELLS \
                    and any(_DASH_C.match(c) for c in command[:-1]):
                # `["sh", "-c", "da-tools …"]`: the string is the command line
                k = next(i for i, c in enumerate(command) if _DASH_C.match(c))
                out.append((idx, ["sh", "-c", command[k + 1]]))
                command = None
        elif key == "args":
            args = _yaml_list(value, block[idx + 1:], indent)
            if args is None:
                continue
            if command and command[0].rsplit("/", 1)[-1] in _SHELLS \
                    and _DASH_C.match(command[-1]) and args:
                # `command: ["/bin/sh", "-c"]` + `args: ["da-tools …"]`
                out.append((idx, ["sh", "-c", args[0]]))
            elif command and command[0] == "da-tools":
                out.append((idx, command[1:] + args))
            elif command is None and image_is_datools:
                out.append((idx, args))
            elif command and image_is_datools and not compose and stats is not None:
                # k8s `command: ["python3", …]` + `args:` under a da-tools image
                stats["cmd_entrypoint_override"] += 1
            command = None
    _flush()
    return out


def scan_commands(doc: Path, rel: str, ctx: _Ctx, errors: list[str]) -> list[Finding]:
    """Fenced lines, inline spans and manifest argvs of one document."""
    findings: list[Finding] = []
    lines = doc.read_text(encoding="utf-8").splitlines()
    for number, text in _logical_lines(lines):
        code, comment = split_shell_comment(text)
        reason: str | None = None
        m = _FENCE_IGNORE.match(comment) if comment is not None else None
        if m:
            reason = _ignore_reason(m)
            if not reason:
                errors.append(f"{rel}:{number}: `{INLINE_IGNORE}` without a reason — "
                              f"write `# {INLINE_IGNORE}: <why this line is exempt>`")
                continue
        tokens = tokenize(code)
        if tokens is None:
            ctx.stats["cmd_unparseable"] += 1
            continue
        if reason is not None:
            ctx.stats["ignored"] += 1
        findings += _dedupe(judge_tokens(tokens, rel, number, ctx, "cmd_segments"), reason)
    for start, block in _fence_blocks(lines):
        for offset, argv in manifest_argvs(block, ctx.stats):
            ctx.stats["cmd_manifest_argvs"] += 1
            if argv[:2] == ["sh", "-c"]:
                inner = tokenize(argv[2]) if len(argv) > 2 else None
                if inner is None:
                    ctx.stats["cmd_unparseable"] += 1
                    continue
                tokens = inner
            else:
                tokens = ["da-tools"] + argv
            findings += _dedupe(judge_tokens(tokens, rel, start + offset, ctx,
                                             "cmd_segments"), None)
    for number, line, fence in walk_lines(lines):
        if fence is not None or "`" not in line:
            continue
        reason = None
        m = _ROW_IGNORE.search(line)
        if m:
            reason = _ignore_reason(m)
            line = line[:m.start()] + line[m.end():]
            if not reason:
                errors.append(f"{rel}:{number}: `{INLINE_IGNORE}` without a reason — "
                              f"write `<!-- {INLINE_IGNORE}: <why> -->`")
                continue
        if reason is not None:
            ctx.stats["ignored"] += 1
        for span in _SPAN.findall(line):
            tokens = tokenize(span)
            if tokens is None:
                ctx.stats["cmd_unparseable"] += 1
                continue
            findings += _dedupe(judge_tokens(tokens, rel, number, ctx, "inline_spans"),
                                reason)
    return findings


def _dedupe(found: list[Finding], reason: str | None) -> list[Finding]:
    if reason is not None:
        found = [f._replace(ignored=reason) for f in found]
    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for f in found:
        if (f.verdict, f.command, f.token) not in seen:
            seen.add((f.verdict, f.command, f.token))
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Carriers B and C: cli-reference option tables and exit-code tables
# ---------------------------------------------------------------------------
class ReferenceFacts(NamedTuple):
    option_rows: int
    exit_codes: int
    sections: set[str]          # commands with a `####` section in this doc
    exit_tables: set[str]       # commands with an exit-code table in this doc
    unmatched_sections: int     # `####` headings that name no command


def scan_reference(doc: Path, rel: str, parsers: dict[str, ParserModel],
                   injected_for: dict[str, frozenset[str]],
                   exit_codes: dict[str, ExitCodes],
                   stats: dict[str, int], errors: list[str],
                   by_header: dict[tuple[str, ...], int],
                   ) -> tuple[list[Finding], ReferenceFacts]:
    findings: list[Finding] = []
    command: str | None = None
    header: tuple[str, ...] | None = None      # the recognised kind, or None
    pending: tuple[str, ...] | None = None     # the last row's cells
    table_header: tuple[str, ...] | None = None  # the row before the separator
    documented: dict[str, set[int]] = {}
    exit_table_line: dict[str, int] = {}
    sections: set[str] = set()
    unmatched = 0
    option_rows = exit_count = 0
    for number, raw, fence in walk_lines(doc.read_text(encoding="utf-8").splitlines()):
        if fence is not None:
            # Fenced content is not a table and its `# comment` lines are
            # not headings: a shell comment in an example block must not
            # end the command's section.
            continue
        heading = _HEADING.match(raw)
        if heading:
            command = heading.group(1)
            sections.add(command)
            header = pending = table_header = None
            continue
        if _H4.match(raw) or _SECTION_END.match(raw):
            # A `####` that names no command (`#### Rollback 程序`) or a
            # higher-level heading ends the last command's section.
            if _H4.match(raw):
                unmatched += 1
            command = None
            header = pending = table_header = None
            continue
        if not raw.startswith("|"):
            continue
        reason: str | None = None
        m = _ROW_IGNORE.search(raw)
        if m:
            reason = _ignore_reason(m)
            raw = raw[:m.start()].rstrip()
            if not reason:
                errors.append(f"{rel}:{number}: `{INLINE_IGNORE}` without a reason — "
                              f"write `<!-- {INLINE_IGNORE}: <why> -->`")
                continue
        cells = _cells(raw)
        key = tuple(c.strip("* ") for c in cells)
        if key in _OPTION_HEADERS or key in _EXIT_HEADERS:
            header = pending = key
            if key in _EXIT_HEADERS and command is not None:
                exit_table_line[command] = number
                documented.setdefault(command, set())
            continue
        if set("".join(cells)) <= set("-: "):
            # A separator after an UNRECOGNISED header ends the previous
            # table's reading; otherwise its rows would be scored under the
            # header of the table above.
            table_header = pending
            header = pending if (pending in _OPTION_HEADERS
                                 or pending in _EXIT_HEADERS) else None
            continue
        pending = key
        if command is None:
            continue
        if header is None:
            if _FLAG_ROW.match(cells[0]) or _CODE_CELL.match(cells[0]):
                errors.append(
                    f"{rel}:{number}: flag/code row under an unrecognised table "
                    f"header {table_header!r} under #### {command} — add that header to "
                    f"check_cli_default_drift's _HEADER_DEFAULT_COL or "
                    f"_HEADER_NO_DEFAULT. Leaving it unmapped removes the table "
                    f"from this check with no red anywhere.")
            continue
        if header in _EXIT_HEADERS:
            if _CODE_CELL.match(cells[0]):
                documented.setdefault(command, set()).update(
                    int(c) for c in re.findall(r"\d+", cells[0]))
            continue
        # option table row
        flags = _FLAG_IN_CELL.findall(cells[0])
        model = parsers.get(command)
        if not flags:
            stats["table_rows_positional"] += 1
            continue
        if model is None:
            stats["table_rows_no_parser"] += 1
            continue
        options = _all_options(model)
        injected = injected_for.get(command, frozenset())
        row: list[Finding] = []
        for flag in flags:
            stats["scored"] += 1
            option_rows += 1
            by_header[header] = by_header.get(header, 0) + 1
            if flag in options or flag in _PARSER_GLOBAL or flag in injected:
                continue
            hits = sorted(o for o in options if o.startswith("--") and o.startswith(flag)) \
                if flag.startswith("--") else []
            if len(hits) == 1:
                row.append(Finding(
                    "V2", rel, number, command, flag,
                    f"option table lists `{flag}`, which `{command}` accepts only as "
                    f"an abbreviation of `{hits[0]}` (#1514). Spell it out — and "
                    f"re-read the row's description against `{hits[0]}`'s real "
                    f"meaning (a directory vs a file), which is what the "
                    f"abbreviation was hiding."))
            else:
                row.append(Finding(
                    "V3", rel, number, command, flag,
                    f"option table lists `{flag}` but `{command}` declares no such "
                    f"flag (#1619) — a reader will build on a capability that does "
                    f"not exist. Remove the row or point it at the real flag."))
        if reason is not None:
            stats["ignored"] += 1
            row = [f._replace(ignored=reason) for f in row]
        findings.extend(row)
    # Carrier C: compare documented codes against reachable codes
    for cmd, codes in documented.items():
        ec = exit_codes.get(cmd)
        if ec is None:
            continue
        judged = {c for c in ec.reachable if c != 0}
        if not judged and ec.undecidable:
            continue
        if cmd in parsers:
            judged.add(EXIT_CALLER_ERROR)
        for code in sorted(judged):
            stats["scored"] += 1
            exit_count += 1
            if code in codes:
                continue
            where = ", ".join(f"line {n}" for n in ec.reachable.get(code, [])) \
                or "argparse (rc 2 on any bad argument)"
            findings.append(Finding(
                "V4", rel, exit_table_line.get(cmd, 0), cmd, str(code),
                f"`{cmd}` can exit {code} ({where}) but its exit-code table lists "
                f"only {sorted(codes)} (#1416). Add the row."))
    return findings, ReferenceFacts(option_rows, exit_count, sections,
                                    set(documented), unmatched)


def _all_options(model: ParserModel) -> frozenset[str]:
    out = set(model.options)
    for sub in (model.subparsers or {}).values():
        out |= _all_options(sub)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Baseline ledger
# ---------------------------------------------------------------------------
class BaselineEntry(NamedTuple):
    file: str
    command: str
    verdict: str
    token: str
    count: int
    ticket: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.file, self.command, self.verdict, self.token)


_ENTRY_KEYS = {"file", "command", "verdict", "token", "count", "ticket"}


def load_baseline(path: Path = BASELINE_PATH) -> tuple[list[BaselineEntry], list[str]]:
    """Ledger entries plus FATAL errors about the ledger's own shape."""
    import yaml
    errors: list[str] = []
    if not path.is_file():
        return [], [f"baseline ledger {_rel(path, REPO_ROOT)} "
                    f"is missing — this check runs against an empty ledger only "
                    f"if you create the file with `entries: []`"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [], [f"baseline ledger is not valid YAML: {exc}"]
    rows = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], ["baseline ledger must be a mapping with an `entries` list"]
    entries: list[BaselineEntry] = []
    seen: set[tuple[str, str, str, str]] = set()
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != _ENTRY_KEYS:
            errors.append(f"baseline entry {idx}: must have exactly the keys "
                          f"{'/'.join(sorted(_ENTRY_KEYS))}, got {row!r}")
            continue
        count = row["count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            errors.append(f"baseline entry {idx}: count must be a positive integer, "
                          f"got {count!r}")
            count = 1
        entry = BaselineEntry(str(row["file"]), str(row["command"]),
                              str(row["verdict"]), str(row["token"]), count,
                              str(row["ticket"]))
        if entry.verdict not in VERDICTS:
            errors.append(f"baseline entry {idx}: verdict {entry.verdict!r} is not "
                          f"one of {VERDICTS}")
        if not _TICKET_RE.match(entry.ticket):
            errors.append(f"baseline entry {idx} ({entry.file} {entry.command} "
                          f"{entry.verdict} {entry.token}): ticket {entry.ticket!r} "
                          f"must be `#<number>` — a real content issue, not a "
                          f"placeholder")
        if entry.key() in seen:
            errors.append(f"baseline entry {idx}: duplicate of an earlier entry "
                          f"{entry.key()}")
        seen.add(entry.key())
        entries.append(entry)
    return entries, errors


def apply_baseline(findings: list[Finding], entries: list[BaselineEntry]
                   ) -> tuple[list[Finding], list[Finding], list[str]]:
    """(open findings, suppressed findings, hard errors).

    Set equality, not containment: an entry suppresses exactly ``count``
    findings with its key (the first ``count`` by line). Fewer live findings
    than ``count`` is stale (content was fixed — lower or delete the row);
    more is new debt (the surplus stays red). An entry whose key is also
    inline-ignored is a double exemption (pick one).
    """
    by_key: dict[tuple[str, str, str, str], list[Finding]] = {}
    ignored_by_key: dict[tuple[str, str, str, str], int] = {}
    for f in findings:
        if f.ignored is not None:
            ignored_by_key[f.key()] = ignored_by_key.get(f.key(), 0) + 1
        else:
            by_key.setdefault(f.key(), []).append(f)
    errors: list[str] = []
    suppressed: list[Finding] = []
    surplus: dict[int, str] = {}
    for e in entries:
        hits = sorted(by_key.get(e.key(), []), key=lambda f: f.line)
        ignored = ignored_by_key.get(e.key(), 0)
        if len(hits) < e.count and ignored:
            # per FINDING, not per key: one site in the ledger and another
            # under an inline ignore is one exemption each and is fine; the
            # ledger claiming MORE than the un-ignored findings while some
            # are ignored means one finding is exempted twice.
            errors.append(f"baseline entry {e.key()} ({e.ticket}): count {e.count} "
                          f"but only {len(hits)} un-ignored finding(s) remain and "
                          f"{ignored} carry an inline `{INLINE_IGNORE}` — one "
                          f"exemption per finding; drop one")
        elif len(hits) < e.count:
            errors.append(f"stale baseline entry {e.key()} ({e.ticket}): count is "
                          f"{e.count} but only {len(hits)} such finding(s) remain — "
                          f"lower the count (delete the row at 0) so the ledger only "
                          f"holds live debt")
        suppressed.extend(hits[:e.count])
        lines = ", ".join(str(f.line) for f in hits)
        for n, f in enumerate(hits[e.count:], e.count + 1):
            # The surplus is reported on the later sites, but the message
            # names every site with this key: the NEW one may be the earlier
            # line, and the author must pick it out, not guess.
            surplus[id(f)] = (f"{f.message} [{n} of {len(hits)} with this key in "
                              f"this file (lines {lines}); the ledger row ({e.ticket}) "
                              f"covers {e.count} of them — the rest are new]")
    done = {id(f) for f in suppressed}
    open_findings = [f._replace(message=surplus.get(id(f), f.message))
                     for f in findings if f.ignored is None and id(f) not in done]
    return open_findings, suppressed, errors


_PLAIN_SCALAR = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")


def _scalar(s: str) -> str:
    """A path or command name as a YAML plain scalar; anything else JSON-quoted."""
    return s if _PLAIN_SCALAR.fullmatch(s) else json.dumps(s)


def write_baseline(findings: list[Finding], existing: list[BaselineEntry],
                   path: Path = BASELINE_PATH) -> int:
    """Regenerate the ledger from *findings*, keeping known tickets."""
    tickets = {e.key(): e.ticket for e in existing}
    counts: dict[tuple[str, str, str, str], int] = {}
    for f in findings:
        if f.ignored is None:
            counts[f.key()] = counts.get(f.key(), 0) + 1
    lines = [
        "# cli-contract-baseline.yaml — 既有內容票在 check_cli_contract 下的紅（#1379）。",
        "# 每列一個 (file, command, verdict, token)，count = 該鍵在該檔的 finding 數；",
        "# 比對是集合相等：少於 count 是 stale 硬錯、多出來的是新紅。",
        "# 修好一處就降 count（歸零就刪列）。ticket 必須是 #NNNN。",
        "# 產生：python3 scripts/tools/lint/check_cli_contract.py --write-baseline",
        "entries:",
    ]
    q = json.dumps      # a JSON string is a YAML double-quoted scalar, escapes included
    for key in sorted(counts):
        file, command, verdict, token = key
        lines.append(f'  - {{file: {_scalar(file)}, command: {_scalar(command)}, '
                     f'verdict: {verdict}, token: {q(token)}, count: {counts[key]}, '
                     f'ticket: {q(tickets.get(key, "#TODO"))}}}')
    if not counts:
        lines[-1] = "entries: []"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return len(counts)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def doc_files(repo_root: Path = REPO_ROOT) -> tuple[list[Path], list[str]]:
    """Scanned docs and the landing pages that are missing.

    Symlinks are skipped: `docs/CHANGELOG.md` points at the root CHANGELOG,
    whose historical entries legitimately quote flags that have since been
    renamed.
    """
    docs = [f for f in sorted((repo_root / "docs").rglob("*.md"))
            if "/internal/" not in f.as_posix() and "/archive/" not in f.as_posix()
            and not f.is_symlink()]
    missing = [rel for rel in EXTRA_DOC_FILES if not (repo_root / rel).is_file()]
    present = [repo_root / rel for rel in EXTRA_DOC_FILES if (repo_root / rel).is_file()]
    return docs + present, missing


def _unscanned_carriers(repo_root: Path = REPO_ROOT) -> int:
    """Files outside this check's scan set that mention the da-tools binary."""
    n = 0
    for pattern in ("tools/portal/src/**/*.js", "tools/portal/src/**/*.jsx",
                    "scripts/**/*.sh", "try-local/**/*.sh"):
        for f in repo_root.glob(pattern):
            try:
                if "da-tools" in f.read_text(encoding="utf-8", errors="ignore"):
                    n += 1
            except OSError:
                continue
    return n


_STAT_KEYS = (
    "scored", "cmd_segments", "inline_spans", "cmd_manifest_argvs", "cmd_sh_c_strings",
    "cmd_no_parser", "cmd_script_not_in_map", "cmd_entrypoint_override",
    "cmd_run_without_image", "cmd_bare_not_command", "cmd_outside_command_position",
    "cmd_positionals",
    "cmd_placeholder_flags", "cmd_placeholder_subcommands", "cmd_skipped_values",
    "cmd_unparseable", "table_rows_positional", "table_rows_no_parser",
    "reference_sections_unmatched", "exit_tables_no_script",
    "exit_undecidable_scripts", "commands_without_exit_table", "ignored",
    "unscanned_carrier_files")


def _new_stats() -> dict[str, int]:
    return {k: 0 for k in _STAT_KEYS}


class ScanResult(NamedTuple):
    findings: list[Finding]          # ignored ones carry their reason
    stats: dict[str, int]
    errors: list[str]                # content-side: rc 1 under --ci
    fatal: list[str]                 # the gate could not do its job: rc 2
    by_header: dict[tuple[str, ...], int]
    per_doc: dict[str, dict[str, int]]


def scan(parsers: dict[str, ParserModel] | None = None,
         command_map: dict[str, str] | None = None,
         docs: list[Path] | None = None,
         reference_docs: tuple[Path, ...] = CLI_REFERENCE_DOCS,
         injected: set[str] | None = None,
         exit_codes: dict[str, ExitCodes] | None = None,
         repo_root: Path = REPO_ROOT,
         ) -> ScanResult:
    errors: list[str] = []
    fatal: list[str] = []
    stats = _new_stats()
    if command_map is None:
        command_map = parse_command_map()
    if parsers is None:
        parsers, unscoreable, blind, faults = introspect_parsers()
        fatal += [f"could not load {b} — this check saw NOTHING for that command"
                  for b in blind] + list(faults)
    elif not command_map:
        fatal.append("COMMAND_MAP parsed to zero commands — nothing to check "
                     "against, so a clean result here means nothing")
    if injected is None:
        injected = prometheus_commands()
    injected_for = {c: frozenset({"--prometheus"}) for c in injected}
    if exit_codes is None:
        exit_codes = {}
        for command, script in command_map.items():
            path = _resolve(script)
            if path is not None:
                exit_codes[command] = reachable_exit_codes(
                    path.read_text(encoding="utf-8"))
    if docs is None:
        docs, missing = doc_files(repo_root)
        fatal += [f"{rel}: listed in EXTRA_DOC_FILES but not found — point the "
                  f"tuple at the file's current path; deleting the entry stops "
                  f"the page being scanned at all" for rel in missing]
    ctx = _Ctx(command_map, parsers, injected_for, stats)
    findings: list[Finding] = []
    for doc in docs:
        findings += scan_commands(doc, _rel(doc, repo_root), ctx, errors)
    by_header: dict[tuple[str, ...], int] = {}
    per_doc: dict[str, dict[str, int]] = {}
    no_table: set[str] = set()
    no_script: set[str] = set()
    for doc in reference_docs:
        rel = _rel(doc, repo_root)
        found, facts = scan_reference(doc, rel, parsers, injected_for, exit_codes,
                                      stats, errors, by_header)
        findings += found
        per_doc[rel] = {"option_rows": facts.option_rows, "exit_codes": facts.exit_codes}
        stats["reference_sections_unmatched"] += facts.unmatched_sections
        no_table |= {c for c in facts.sections
                     if c in parsers and c not in facts.exit_tables}
        no_script |= {c for c in facts.exit_tables if c not in exit_codes}
        for carrier, label in (("option_rows", "option-table flags"),
                               ("exit_codes", "exit codes")):
            if not per_doc[rel][carrier]:
                fatal.append(f"{rel} contributed 0 judged {label} — this check "
                             f"read the file and compared nothing in it")
    # Disclosure counts are SETS of commands, computed once: a script seen in
    # both reference docs is one script.
    stats["commands_without_exit_table"] = len(no_table)
    stats["exit_tables_no_script"] = len(no_script)
    stats["exit_undecidable_scripts"] = len(
        {c for c, ec in exit_codes.items() if ec.undecidable})
    if not stats["scored"]:
        fatal.append("0 judged tokens — every command, flag and exit code was "
                     "skipped, so this check compared nothing")
    stats["unscanned_carrier_files"] = _unscanned_carriers(repo_root)
    return ScanResult(findings, stats, errors, fatal, by_header, per_doc)


def _rel(doc: Path, repo_root: Path) -> str:
    try:
        return doc.relative_to(repo_root).as_posix()
    except ValueError:
        return doc.as_posix()


def _not_scored_lines(stats: dict[str, int]) -> list[str]:
    s = stats
    return [
        f"scanned: {s['cmd_segments']} fenced command segments, {s['inline_spans']} "
        f"inline code spans, {s['cmd_manifest_argvs']} manifest args lists, "
        f"{s['cmd_sh_c_strings']} `sh -c` strings",
        f"NOT scored: {s['cmd_no_parser']} commands under a subcommand with no "
        f"argparse parser (guard/parser/batch-pr — their subcommands are "
        f"check_doc_datools_cmds' job), {s['cmd_script_not_in_map']} `python3 "
        f"scripts/tools/…` scripts not in COMMAND_MAP, "
        f"{s['cmd_outside_command_position']} `da-tools <subcommand>` invocations "
        f"(or `sh -c \"da-tools …\"` strings) outside command position — after a "
        f"wrapper this check does not model (`docker exec <ctr>`, `timeout N`, "
        f"`xargs -n N`), inside a box diagram, or in a checklist item, "
        f"{s['cmd_bare_not_command']} `da-tools` words neither in command position "
        f"nor followed by a subcommand (image, release or container names), "
        f"{s['cmd_run_without_image']} `docker/kubectl run` "
        f"segments whose image could not be recognised (`$IMAGE`, `--image=`), "
        f"{s['cmd_entrypoint_override']} entrypoint overrides (`docker run "
        f"--entrypoint`, k8s `command:` / compose `entrypoint:` under a da-tools "
        f"image), "
        f"{s['cmd_positionals']} positional tokens, {s['cmd_placeholder_flags']} "
        f"placeholder flags, {s['cmd_placeholder_subcommands']} placeholder "
        f"subcommands, {s['cmd_skipped_values']} words skipped as the value of an "
        f"undeclared flag, {s['cmd_unparseable']} unparseable lines/spans, "
        f"{s['table_rows_positional']} option-table rows without a flag, "
        f"{s['table_rows_no_parser']} option-table rows under a no-parser "
        f"subcommand, {s['reference_sections_unmatched']} `####` headings naming no "
        f"command, {s['exit_tables_no_script']} exit-code tables with no script, "
        f"{s['exit_undecidable_scripts']} scripts with an opaque exit expression "
        f"(judged only on what resolved), {s['commands_without_exit_table']} "
        f"commands with a parser and a reference section but no exit-code table, "
        f"{s['ignored']} lines/rows under `{INLINE_IGNORE}`, "
        f"{s['unscanned_carrier_files']} non-markdown files mentioning da-tools "
        f"(portal JS/JSX, shell scripts) outside this scan set",
        "⚠️ NOT scored is a disclosure, not coverage: this check cannot see those.",
    ]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="documented da-tools invocations vs the live argparse contract (#1379)")
    ap.add_argument("--ci", action="store_true", help="Exit non-zero on findings")
    ap.add_argument("--json", action="store_true", help="One JSON document on stdout")
    ap.add_argument("--write-baseline", action="store_true",
                    help="Regenerate docs/internal/cli-contract-baseline.yaml from "
                         "the current findings (new rows get ticket #TODO)")
    args = ap.parse_args()

    for doc in CLI_REFERENCE_DOCS:
        if not doc.is_file():
            if args.json:
                json.dump({"findings": [], "suppressed": 0, "ignored": 0,
                           "stats": {}, "errors": [], "fatal": [f"missing {doc}"]},
                          sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
            print(f"ERROR: missing {doc}", file=sys.stderr)
            return EXIT_CALLER_ERROR

    result = scan()
    findings, stats = result.findings, result.stats
    entries, ledger_errors = load_baseline()
    if args.write_baseline:
        if result.fatal:
            # A ledger regenerated on a machine where a parser did not load
            # would silently drop every row of that command.
            for e in result.fatal:
                print(f"[FATAL] {e}", file=sys.stderr)
            print("refusing to write the baseline: the scan did not complete",
                  file=sys.stderr)
            return EXIT_CALLER_ERROR
        n = write_baseline(findings, entries)
        print(f"wrote {n} entries to {_rel(BASELINE_PATH, REPO_ROOT)} "
              f"(fill every `#TODO` ticket; the check rejects placeholders)")
        return EXIT_OK
    fatal = result.fatal + ledger_errors
    open_findings, suppressed, baseline_errors = apply_baseline(findings, entries)
    errors = result.errors + baseline_errors
    ignored = [f for f in findings if f.ignored is not None]

    if args.json:
        json.dump({"findings": [f._asdict() for f in open_findings],
                   "suppressed": len(suppressed), "ignored": len(ignored),
                   "stats": stats, "errors": errors, "fatal": fatal},
                  sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
        for e in fatal:
            print(f"[FATAL] {e}", file=sys.stderr)
        for e in errors:
            print(f"[ERROR] {e}", file=sys.stderr)
        for f in open_findings:
            print(f"[{f.verdict}] {f.file}:{f.line}  {f.command} {f.token}")
            print(f"       {f.message}")
        print()
        print(f"judged {stats['scored']} tokens; {len(open_findings)} open finding(s), "
              f"{len(suppressed)} suppressed by the baseline ledger, "
              f"{len(ignored)} inline-ignored")
        for line in _not_scored_lines(stats):
            print(line)
        if not open_findings and not errors and not fatal:
            print("✅ every documented da-tools invocation matches the CLI contract")
    if fatal:
        # The apparatus did not run to completion: a caller/setup error, never
        # to be confused with "checked and found N things".
        return EXIT_CALLER_ERROR
    if errors or open_findings:
        return EXIT_VIOLATION if args.ci else EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
