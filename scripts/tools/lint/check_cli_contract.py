#!/usr/bin/env python3
"""check_cli_contract.py — 文件裡教人抄的 da-tools 命令 ↔ 活的 argparse 契約（#1379 / TRK-370）。

判定（每條都從兩個獨立來源推導後比對，不列舉壞拼法）：

  V0  fence 內 ``da-tools <x>`` 的 ``x`` 不是 ``COMMAND_MAP`` 的鍵
  V1  fence 內的旗標不在該 subcommand 的 parser ``option_strings`` 裡（含
      二層 argparse 子命令不在 ``add_subparsers`` 的 choices）
  V2  fence 內的旗標不是精確名、卻是恰一個長旗標的前綴——argparse 預設
      ``allow_abbrev`` 會收下它並綁到另一個旗標、rc=0（#1514）。owner 裁決
      一律違規，不分有害無害：今天無害只是因為還沒有第二個同前綴的旗標
  V3  cli-reference 選項表第一欄的旗標不在 parser（#1619）
  V4  script 以 AST 可達的非零結束碼，不在 cli-reference 該命令節的結束碼表
      （#1416；只判「可達但未列」，不判 ``0``、不判「列了但 AST 看不到」）

契約來源：``_lint_helpers.parse_command_map`` 給 subcommand→script；每支 script
用 runpy 跑到 ``parse_args`` 被攔下為止，拿到真的 ``ArgumentParser``（動態加入
的參數也在；``--help`` 文字看不到沒有 help 的參數，CPython gh-95889）；
``--prometheus`` 由 entrypoint 對 ``PROMETHEUS_COMMANDS`` 注入，從 entrypoint
的 AST 讀。

分工：``guard`` / ``parser`` / ``batch-pr`` 走 ``GoBinaryDispatcher``，runpy 在
argparse 之前就 SystemExit，本閘門把它們歸「無 parser」通道、不判旗標、只揭露
計數；它們的子命令合法性由既有 ``check_doc_datools_cmds`` 負責（本閘門不重做）。
portal 的 ``commands.js`` / ``platform-demo.jsx`` 不在本閘門掃描面（另一支 PR），
同樣只揭露。

帳本：``docs/internal/cli-contract-baseline.yaml`` 列既有內容票的紅
（``file`` + ``command`` + ``verdict`` + ``token`` + ``ticket``）；帳本外的紅
就是紅，帳本內**不再重現**的列是 stale 硬錯，``ticket`` 必須是 ``#NNNN``。
逃生門：fence 行尾 ``# datools-cmd-ignore: <理由>``、表格列尾
``<!-- datools-cmd-ignore: <理由> -->``；理由空是硬錯，帳本與 ignore 同時命中
同一 finding 是硬錯，被 ignore 的行計入揭露。

⚠️ NOT scored 是揭露不是涵蓋：無 parser 的命令、不在 COMMAND_MAP 的
script、位置參數、佔位符旗標、不可判的 exit 表達式、被 ignore 的行、以及
非 markdown 載體，這支工具都看不到，計數印在輸出末尾。
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import re
import runpy
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, NamedTuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lint_helpers import ENTRYPOINT_PATH, parse_command_map  # noqa: E402
# ⛔ Imported, not copied: the header pin, the env scrub set and the
# scan-surface identity are the SAME facts #1556 already established. A second
# transcription of any of them is a second thing that can drift (D-05g: read
# the declared set, do not keep a copy).
from check_cli_default_drift import (  # noqa: E402
    _HEADER_DEFAULT_COL, _HEADER_NO_DEFAULT, _SCRUBBED_ENV, _resolve,
    _scan_surface_faults,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_REFERENCE_DOCS = (REPO_ROOT / "docs" / "cli-reference.md",
                      REPO_ROOT / "docs" / "cli-reference.en.md")
BASELINE_PATH = REPO_ROOT / "docs" / "internal" / "cli-contract-baseline.yaml"
# Customer landing pages outside docs/. Enumerated by FILE (same tuple and
# same reasoning as check_doc_datools_cmds); a missing one is a hard error,
# never a silent shrink.
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

_HEADING = re.compile(r"^####\s+`?([A-Za-z][\w-]*)`?\s*$")
_SECTION_END = re.compile(r"^#{1,3}\s")
_FLAG_IN_CELL = re.compile(r"(?<![\w./-])(--?[A-Za-z][\w-]*)")
# A first cell that IS a flag spec: an optional single code span whose text
# starts with a flag. `\`--json\` 的 \`coverage[]\`` (two spans, prose between)
# is a description that mentions a flag, not a flag row — measured under an
# unrecognised `| 輸出 | 內容 |` header, where the looser "contains a flag"
# test turned that prose into a hard error with no legal way to go green.
_FLAG_ROW = re.compile(r"^`?-{1,2}[A-Za-z][\w-]*(?:[^`]*`)?\s*$")
_CODE_CELL = re.compile(r"^`?(\d+)`?$")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_ROW_IGNORE = re.compile(r"<!--\s*" + INLINE_IGNORE + r"\s*:?\s*(.*?)\s*-->")
_FENCE_IGNORE = re.compile(INLINE_IGNORE + r"\s*:?\s*(.*)$")
_IMAGE_REF = re.compile(r"^(?:[\w.-]+/)*da-tools(?:[:@][^\s\\]+)?$")
_PY_INTERPRETERS = frozenset({"python", "python3", "py"})
_REDIRECT = re.compile(r"^(\d*>|<)")
_NEGATIVE_NUMBER = re.compile(r"^-\d")
_PLACEHOLDER_CHARS = "<>${}[]…"
_SHELL_PUNCT = "();|&"
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


def reachable_exit_codes(source: str) -> ExitCodes:
    """Non-zero exit codes a script can produce, by AST.

    Resolves ``sys.exit(<expr>)`` / ``raise SystemExit(<expr>)`` /
    ``die_caller_error(...)`` (=2), where ``<expr>`` is an ``EXIT_*`` name, an
    int literal, an ``IfExp`` (both arms), a module-level name bound to an int
    literal, or a module-level function whose ``return`` statements resolve by
    the same rules (``sys.exit(main())``). Anything else is undecidable and
    disclosed with its line. argparse's own rc 2 is not looked for here: the
    caller adds it for every command that HAS a parser.
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
            fn = funcs[expr.func.id]
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Return):
                    _resolve_expr(sub.value, sub.lineno, depth + 1)
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

    ⛔ Not the sibling's ``_cells``: that one splits on every ``|``, so a
    flag cell such as ``\`--format md\|json\``` is cut in two and its first
    fragment is an unterminated code span. Header keys are unaffected (no
    header carries an escaped pipe), which is why the two readers can
    disagree here without disagreeing on which table they are in.
    """
    return [c.strip() for c in _UNESCAPED_PIPE.split(line.strip().strip("|"))]


def _unquote_md(line: str) -> str:
    s = line.lstrip()
    return s[2:] if s.startswith("> ") else (s[1:] if s.startswith(">") else line)


def _is_fence(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("```") or s.startswith("~~~")


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


def _is_placeholder(tok: str) -> bool:
    return any(c in tok for c in _PLACEHOLDER_CHARS) or tok == "..."


def _looks_like_flag(tok: str) -> bool:
    return tok.startswith("-") and tok != "-" and tok != "--" \
        and not _NEGATIVE_NUMBER.match(tok)


def tokenize(code: str) -> list[str] | None:
    lex = shlex.shlex(code, posix=True, punctuation_chars=_SHELL_PUNCT)
    lex.whitespace_split = True
    try:
        return [t for t in lex if t != "\\"]
    except ValueError:
        return None


def _truncate_at_separator(tokens: list[str]) -> list[str]:
    """The argv of the FIRST command in *tokens*.

    Control operators (``|`` ``&&`` ``;`` ``&`` ``(`` ``)``) and a heredoc end
    the command. A redirection (``> f``, ``>f``, ``2>/dev/null``, ``< f``) is
    dropped together with its target and the walk CONTINUES — the shell still
    hands ``--flag`` in ``cmd > out.json --flag`` to ``cmd``, so stopping there
    would silently un-judge everything after the redirect. ``<tenant>`` is a
    placeholder, not a redirect: it closes with ``>``.
    """
    out: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            # `2>&1` tokenises as `2>` `&` `1`: the `&` is a dup marker here,
            # not a control operator, and the fd after it is its target.
            skip_next = tok == "&"
            continue
        if set(tok) <= set(_SHELL_PUNCT) or tok.startswith("<<"):
            break
        if _REDIRECT.match(tok) and not (tok.startswith("<") and ">" in tok):
            # bare operator (`>`, `>>`, `2>`, `<`): the target is the next token
            skip_next = tok.rstrip("<>0123456789") == ""
            continue
        out.append(tok)
    return out


class Subject(NamedTuple):
    kind: str          # "da-tools" | "image" | "python"
    command: str | None
    args: list[str]
    note: str | None   # disclosure when command is None


def find_subject(tokens: list[str], command_map: dict[str, str]) -> Subject | None:
    """The da-tools argv in *tokens*, or None when no subject token is present.

    Three forms: the bare binary, an image reference (everything BEFORE it is a
    docker flag), and ``python3 …/<script>.py`` reverse-mapped through
    COMMAND_MAP.
    """
    by_script = {v: k for k, v in command_map.items()}
    for i, tok in enumerate(tokens):
        if tok == "da-tools" or (tok.endswith("/da-tools") and tok[0] in "./~$"):
            # the binary, bare or by path (`./da-tools`, `$HOME/bin/da-tools`)
            return Subject("da-tools", None, tokens[i + 1:], None)
        if _IMAGE_REF.match(tok):
            # ⛔ An image reference is a subject only where the image is RUN.
            # `docker pull/push/tag`, `kind load docker-image` and a YAML
            # `image:` line name the same reference and execute nothing;
            # measured on the tree this landed in, `kind load docker-image
            # da-tools:dev --name …` and `docker tag … internal-registry.corp/
            # da-tools:v2.9.0` both reported their NEXT token as an unknown
            # subcommand. "Something before it is `run`" is the derivable
            # property (docker/podman/nerdctl/`compose run`/`container run`
            # all spell it that way); the enumeration it replaces was "which
            # docker verbs do not run" — a list with no authority behind it.
            if "run" not in tokens[:i]:
                continue      # `docker pull X && da-tools …`: keep looking
            if any(t == "--entrypoint" or t.startswith("--entrypoint=")
                   for t in tokens[:i]):
                return Subject("image", None, tokens[i + 1:],
                               "entrypoint overridden; argv is not da-tools'")
            return Subject("image", None, tokens[i + 1:], None)
        if tok in _PY_INTERPRETERS:
            # interpreter flags (`py -3`, `python3 -X utf8`) sit before the script
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 1 + (tokens[j] in ("-X", "-W", "-m", "-c"))
            if j < len(tokens) and tokens[j].endswith(".py"):
                base = tokens[j].rsplit("/", 1)[-1]
                if base in by_script:
                    return Subject("python", by_script[base], tokens[j + 1:], None)
                if "scripts/tools/" in tokens[j]:
                    return Subject("python", None, tokens[j + 1:],
                                   f"script not in COMMAND_MAP: {base}")
    return None


# ---------------------------------------------------------------------------
# Judging one argv
# ---------------------------------------------------------------------------
def resolve_flag(name: str, model: ParserModel) -> tuple[str | None, str | None]:
    """(verdict, resolved option) for one flag token against one parser.

    verdict None = accepted exactly; "V2" = accepted only as a unique long
    prefix; "V1" = rejected (unknown, or an ambiguous prefix argparse would
    also reject with rc 2).
    """
    if name in model.options or name in _PARSER_GLOBAL:
        return None, name
    if name.startswith("--"):
        hits = sorted(o for o in model.options
                      if o.startswith("--") and o.startswith(name))
        if len(hits) == 1:
            return "V2", hits[0]
        return "V1", None
    if len(name) > 2 and name[:2] in model.options and model.arity.get(name[:2]) != 0:
        return None, name[:2]          # -oVALUE
    return "V1", None


def judge_argv(args: list[str], command: str, model: ParserModel | None,
               injected: frozenset[str], stats: dict[str, int],
               file: str, line: int) -> list[Finding]:
    """Findings for the tokens after the subcommand of one command line."""
    findings: list[Finding] = []
    if model is None:
        stats["fence_no_parser"] += 1
        return findings
    parser = model
    positional_index = 0
    after_double_dash = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1
        if after_double_dash:
            stats["fence_positionals"] += 1
            continue
        if tok == "--":
            after_double_dash = True
            continue
        if _looks_like_flag(tok):
            name, eq, _val = tok.partition("=")
            if _is_placeholder(name):
                stats["fence_placeholder_flags"] += 1
                continue
            if name in injected:
                verdict, resolved, arity = None, name, 1
            else:
                verdict, resolved = resolve_flag(name, parser)
                arity = parser.arity.get(resolved, 0) if resolved else 0
                if resolved is not None and resolved != name:
                    # `-oVALUE`: the value is attached, nothing follows
                    if not name.startswith("--"):
                        arity = 0
            stats["scored"] += 1
            if verdict == "V2":
                findings.append(Finding(
                    "V2", file, line, command, name,
                    f"`{name}` is not a flag of `{command}`; argparse accepts it "
                    f"only as an abbreviation of `{resolved}` (rc=0, bound to the "
                    f"other flag — #1514). Spell it out as `{resolved}`."))
            elif verdict == "V1":
                findings.append(Finding(
                    "V1", file, line, command, name,
                    f"`{command}` does not declare `{name}` — copying this line "
                    f"gives `unrecognized arguments`, rc=2. Use a flag the CLI "
                    f"declares (see `da-tools {command} --help`); a deliberately "
                    f"aspirational example needs `# {INLINE_IGNORE}: <why>`."))
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
                stats["fence_placeholder_subcommands"] += 1
                return findings
            stats["scored"] += 1
            findings.append(Finding(
                "V1", file, line, command, tok,
                f"`{command}` has no action `{tok}` (choices: "
                f"{', '.join(sorted(parser.subparsers))})."))
            return findings
        positional_index += 1
        stats["fence_positionals"] += 1
    return findings


# ---------------------------------------------------------------------------
# Carrier A: fenced commands
# ---------------------------------------------------------------------------
def _logical_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Fenced-block content as (first line number, joined text) pairs."""
    out: list[tuple[int, str]] = []
    in_code = False
    buf: list[str] = []
    start = 0
    for number, raw in enumerate(lines, 1):
        line = _unquote_md(raw)
        if _is_fence(line):
            if buf:
                out.append((start, " ".join(buf)))
                buf = []
            in_code = not in_code
            continue
        if not in_code:
            continue
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


def scan_fences(doc: Path, rel: str, command_map: dict[str, str],
                parsers: dict[str, ParserModel], injected_for: dict[str, frozenset[str]],
                stats: dict[str, int], errors: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    lines = doc.read_text(encoding="utf-8").splitlines()
    for number, text in _logical_lines(lines):
        code, comment = split_shell_comment(text)
        reason: str | None = None
        if comment is not None and INLINE_IGNORE in comment:
            m = _FENCE_IGNORE.search(comment)
            reason = (m.group(1).strip() if m else "")
            if not reason:
                errors.append(f"{rel}:{number}: `{INLINE_IGNORE}` without a reason — "
                              f"write `# {INLINE_IGNORE}: <why this line is exempt>`")
                continue
        tokens = tokenize(code)
        if tokens is None:
            stats["fence_unparseable"] += 1
            continue
        subject = find_subject(tokens, command_map)
        if subject is None:
            continue
        stats["fence_commands"] += 1
        args = _truncate_at_separator(subject.args)
        command = subject.command
        line_findings: list[Finding] = []
        if command is None and subject.note:
            stats["fence_entrypoint_override" if subject.kind == "image"
                  else "fence_script_not_in_map"] += 1
            continue
        if command is None:
            if not args:
                continue
            head = args[0]
            if head in _ENTRYPOINT_GLOBAL:
                continue
            if _is_placeholder(head):
                stats["fence_placeholder_subcommands"] += 1
                continue
            stats["scored"] += 1
            if head not in command_map:
                line_findings.append(Finding(
                    "V0", rel, number, head, head,
                    f"`da-tools {head}` — no such subcommand in COMMAND_MAP "
                    f"(rc=2). Use a real subcommand; a deliberately aspirational "
                    f"example needs `# {INLINE_IGNORE}: <why>`."))
                command = head
            else:
                command = head
                args = args[1:]
        if not line_findings:
            line_findings = judge_argv(
                args, command, parsers.get(command),
                injected_for.get(command, frozenset()), stats, rel, number)
        if reason is not None:
            stats["ignored"] += 1
            line_findings = [f._replace(ignored=reason) for f in line_findings]
        seen: set[tuple[str, str, str]] = set()
        for f in line_findings:
            if (f.verdict, f.command, f.token) not in seen:
                seen.add((f.verdict, f.command, f.token))
                findings.append(f)
    return findings


# ---------------------------------------------------------------------------
# Carriers B and C: cli-reference option tables and exit-code tables
# ---------------------------------------------------------------------------
def scan_reference(doc: Path, rel: str, parsers: dict[str, ParserModel],
                   injected_for: dict[str, frozenset[str]],
                   exit_codes: dict[str, ExitCodes],
                   stats: dict[str, int], errors: list[str],
                   by_header: dict[tuple[str, ...], int],
                   per_doc: dict[str, dict[str, int]]) -> list[Finding]:
    findings: list[Finding] = []
    command: str | None = None
    header: tuple[str, ...] | None = None      # the recognised kind, or None
    pending: tuple[str, ...] | None = None     # the last row's cells
    table_header: tuple[str, ...] | None = None  # the row before the separator
    documented: dict[str, set[int]] = {}
    exit_table_line: dict[str, int] = {}
    counts = per_doc.setdefault(rel, {"option_rows": 0, "exit_codes": 0})
    in_fence = False
    for number, raw in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
        if _is_fence(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            # ⛔ Fenced content is not a table and its `# comment` lines are
            # not headings. Measured without this: a shell comment inside a
            # command's example block matched the section-end rule, reset
            # the command, and the exit-code tables after it were skipped —
            # 61 judged codes fell to 36 (zh) and 14 (en) with no error.
            continue
        heading = _HEADING.match(raw)
        if heading:
            command = heading.group(1)
            header = pending = table_header = None
            continue
        if _SECTION_END.match(raw):
            # A higher-level heading ends the last command's section; the
            # document-tail tables (version matrix, related docs) are not
            # `discover-mappings` content just because it came last.
            command = None
            header = pending = table_header = None
            continue
        if not raw.startswith("|"):
            continue
        reason: str | None = None
        m = _ROW_IGNORE.search(raw)
        if m:
            reason = m.group(1).strip()
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
            # ⛔ A separator after an UNRECOGNISED header ends the previous
            # table's reading. Without this reset the rows of `| 參數 | 用途 |`
            # were judged as if the option table above them continued —
            # measured: four unpinned header shapes showed up in the
            # "productive headers" set, i.e. tables were being scored under a
            # header the pin had never seen.
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
            code = _CODE_CELL.match(cells[0])
            if code:
                documented.setdefault(command, set()).add(int(code.group(1)))
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
            counts["option_rows"] += 1
            by_header[header] = by_header.get(header, 0) + 1
            if flag in options or flag in _PARSER_GLOBAL or flag in injected:
                continue
            hits = sorted(o for o in options if o.startswith("--") and o.startswith(flag)) \
                if flag.startswith("--") else []
            if len(hits) == 1:
                row.append(Finding(
                    "V2", rel, number, command, flag,
                    f"option table lists `{flag}`, which `{command}` accepts only as "
                    f"an abbreviation of `{hits[0]}` (#1514). Spell it out."))
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
            stats["exit_tables_no_script"] += 1
            continue
        if ec.undecidable:
            stats["exit_undecidable_scripts"] += 1
        judged = {c for c in ec.reachable if c != 0}
        if not judged and ec.undecidable:
            continue
        if cmd in parsers:
            judged.add(EXIT_CALLER_ERROR)
        for code in sorted(judged):
            stats["scored"] += 1
            counts["exit_codes"] += 1
            if code in codes:
                continue
            where = ", ".join(f"line {n}" for n in ec.reachable.get(code, [])) \
                or "argparse (rc 2 on any bad argument)"
            findings.append(Finding(
                "V4", rel, exit_table_line.get(cmd, 0), cmd, str(code),
                f"`{cmd}` can exit {code} ({where}) but its exit-code table lists "
                f"only {sorted(codes)} (#1416). Add the row."))
    return findings


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
    ticket: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.file, self.command, self.verdict, self.token)


def load_baseline(path: Path = BASELINE_PATH) -> tuple[list[BaselineEntry], list[str]]:
    """Ledger entries plus hard errors about the ledger's own shape."""
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
        if not isinstance(row, dict) or set(row) != {"file", "command", "verdict",
                                                    "token", "ticket"}:
            errors.append(f"baseline entry {idx}: must have exactly the keys "
                          f"file/command/verdict/token/ticket, got {row!r}")
            continue
        entry = BaselineEntry(*(str(row[k]) for k in
                                ("file", "command", "verdict", "token", "ticket")))
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

    An entry suppresses EVERY open finding with its key. An entry matching
    nothing is stale (the content was fixed — delete the row). An entry whose
    key is also inline-ignored is a double exemption (pick one).
    """
    by_key: dict[tuple[str, str, str, str], list[Finding]] = {}
    ignored_keys: set[tuple[str, str, str, str]] = set()
    for f in findings:
        if f.ignored is not None:
            ignored_keys.add(f.key())
        else:
            by_key.setdefault(f.key(), []).append(f)
    errors: list[str] = []
    suppressed: list[Finding] = []
    matched: set[tuple[str, str, str, str]] = set()
    for e in entries:
        if e.key() in ignored_keys:
            errors.append(f"baseline entry {e.key()} is ALSO covered by an inline "
                          f"`{INLINE_IGNORE}` — one exemption per finding; drop one")
        hits = by_key.get(e.key())
        if not hits:
            errors.append(f"stale baseline entry {e.key()} ({e.ticket}): no such "
                          f"finding any more — delete the row so the ledger only "
                          f"holds live debt")
            continue
        matched.add(e.key())
        suppressed.extend(hits)
    open_findings = [f for f in findings
                     if f.ignored is None and f.key() not in matched]
    return open_findings, suppressed, errors


def write_baseline(findings: list[Finding], existing: list[BaselineEntry],
                   path: Path = BASELINE_PATH) -> int:
    """Regenerate the ledger from *findings*, keeping known tickets."""
    tickets = {e.key(): e.ticket for e in existing}
    keys = sorted({f.key() for f in findings if f.ignored is None})
    lines = [
        "# cli-contract-baseline.yaml — 既有內容票在 check_cli_contract 下的紅（#1379）。",
        "# 每列一個 (file, command, verdict, token)；同鍵的所有 finding 都被壓下。",
        "# 修好一處就刪一列：零命中的列是 stale 硬錯。ticket 必須是 #NNNN。",
        "# 產生：python3 scripts/tools/lint/check_cli_contract.py --write-baseline",
        "entries:",
    ]
    for file, command, verdict, token in keys:
        ticket = tickets.get((file, command, verdict, token), "#TODO")
        lines.append(f'  - {{file: {file}, command: {command}, verdict: {verdict}, '
                     f'token: "{token}", ticket: "{ticket}"}}')
    if not keys:
        lines[-1] = "entries: []"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(keys)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def doc_files(repo_root: Path = REPO_ROOT) -> tuple[list[Path], list[str]]:
    docs = [f for f in sorted((repo_root / "docs").rglob("*.md"))
            if "/internal/" not in f.as_posix() and "/archive/" not in f.as_posix()]
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


def _new_stats() -> dict[str, int]:
    return {k: 0 for k in (
        "scored", "fence_commands", "fence_no_parser", "fence_script_not_in_map",
        "fence_entrypoint_override",
        "fence_positionals", "fence_placeholder_flags", "fence_placeholder_subcommands",
        "fence_unparseable", "table_rows_positional", "table_rows_no_parser",
        "exit_tables_no_script", "exit_undecidable_scripts", "ignored",
        "unscanned_carrier_files")}


def scan(parsers: dict[str, ParserModel] | None = None,
         command_map: dict[str, str] | None = None,
         docs: list[Path] | None = None,
         reference_docs: tuple[Path, ...] = CLI_REFERENCE_DOCS,
         injected: set[str] | None = None,
         exit_codes: dict[str, ExitCodes] | None = None,
         repo_root: Path = REPO_ROOT,
         ) -> tuple[list[Finding], dict[str, int], list[str],
                    dict[tuple[str, ...], int], dict[str, dict[str, int]]]:
    """All findings (ignored ones carry their reason) + stats + hard errors."""
    errors: list[str] = []
    stats = _new_stats()
    if command_map is None:
        command_map = parse_command_map()
    if parsers is None:
        parsers, unscoreable, blind, faults = introspect_parsers()
        errors += [f"could not load {b} — this check saw NOTHING for that command"
                   for b in blind] + list(faults)
    elif not command_map:
        errors.append("COMMAND_MAP parsed to zero commands — nothing to check "
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
        errors += [f"{rel}: listed in EXTRA_DOC_FILES but not found — point the "
                   f"tuple at the file's current path; deleting the entry stops "
                   f"the page being scanned at all" for rel in missing]
    findings: list[Finding] = []
    for doc in docs:
        rel = _rel(doc, repo_root)
        findings += scan_fences(doc, rel, command_map, parsers, injected_for,
                                stats, errors)
    by_header: dict[tuple[str, ...], int] = {}
    per_doc: dict[str, dict[str, int]] = {}
    for doc in reference_docs:
        rel = _rel(doc, repo_root)
        findings += scan_reference(doc, rel, parsers, injected_for, exit_codes,
                                   stats, errors, by_header, per_doc)
        counts = per_doc[rel]
        for carrier, label in (("option_rows", "option-table flags"),
                               ("exit_codes", "exit codes")):
            if not counts[carrier]:
                errors.append(f"{rel} contributed 0 judged {label} — this check "
                              f"read the file and compared nothing in it")
    if not stats["scored"]:
        errors.append("0 judged tokens — every command, flag and exit code was "
                      "skipped, so this check compared nothing")
    stats["unscanned_carrier_files"] = _unscanned_carriers(repo_root)
    return findings, stats, errors, by_header, per_doc


def _rel(doc: Path, repo_root: Path) -> str:
    try:
        return doc.relative_to(repo_root).as_posix()
    except ValueError:
        return doc.as_posix()


def _not_scored_lines(stats: dict[str, int]) -> list[str]:
    return [
        f"NOT scored: {stats['fence_no_parser']} fenced commands under a "
        f"subcommand with no argparse parser (guard/parser/batch-pr — their "
        f"subcommands are check_doc_datools_cmds' job), "
        f"{stats['fence_script_not_in_map']} `python3 scripts/tools/…` scripts "
        f"not in COMMAND_MAP, {stats['fence_positionals']} positional tokens, "
        f"{stats['fence_placeholder_flags']} placeholder flags, "
        f"{stats['fence_placeholder_subcommands']} placeholder subcommands, "
        f"{stats['fence_entrypoint_override']} `docker run --entrypoint` "
        f"overrides, "
        f"{stats['fence_unparseable']} unparseable fenced lines, "
        f"{stats['table_rows_positional']} option-table rows without a flag, "
        f"{stats['table_rows_no_parser']} option-table rows under a no-parser "
        f"subcommand, {stats['exit_tables_no_script']} exit-code tables with no "
        f"script, {stats['exit_undecidable_scripts']} scripts with an opaque "
        f"exit expression (judged only on what resolved), "
        f"{stats['ignored']} lines/rows under `{INLINE_IGNORE}`, "
        f"{stats['unscanned_carrier_files']} non-markdown files mentioning "
        f"da-tools (portal JS/JSX, shell scripts) outside this scan set",
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
                import json
                json.dump({"findings": [], "suppressed": 0, "ignored": 0,
                           "stats": {}, "errors": [f"missing {doc}"]},
                          sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
            print(f"ERROR: missing {doc}", file=sys.stderr)
            return EXIT_CALLER_ERROR

    findings, stats, errors, _by_header, _per_doc = scan()
    entries, ledger_errors = load_baseline()
    if args.write_baseline:
        n = write_baseline(findings, entries)
        print(f"wrote {n} entries to {BASELINE_PATH.relative_to(REPO_ROOT).as_posix()} "
              f"(fill every `#TODO` ticket; the check rejects placeholders)")
        return EXIT_OK
    errors += ledger_errors
    open_findings, suppressed, baseline_errors = apply_baseline(findings, entries)
    errors += baseline_errors
    ignored = [f for f in findings if f.ignored is not None]

    if args.json:
        import json
        json.dump({"findings": [f._asdict() for f in open_findings],
                   "suppressed": len(suppressed), "ignored": len(ignored),
                   "stats": stats, "errors": errors},
                  sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
    else:
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
        if not open_findings and not errors:
            print("✅ every documented da-tools invocation matches the CLI contract")
    if errors or open_findings:
        return EXIT_VIOLATION if args.ci else EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
