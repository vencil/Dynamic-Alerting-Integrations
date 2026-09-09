#!/usr/bin/env python3
"""check_unreachable_pipestatus.py — 找出 ``set -e`` + ``pipefail`` 之下不可達的 ``PIPESTATUS`` 讀取。

Why this exists
---------------
同一個缺陷形狀在這個 repo 出現過**兩次**：

- TRK-376 / #1734 — ``.github/workflows/bench-probe-write-latency.yaml``
- TRK-381 / #1771 — ``scripts/tools/ops/bench_wrapper.sh``

形狀：腳本開頭 ``set -euo pipefail``，跑一條管線，然後在**下一行**讀
``${PIPESTATUS[0]}`` 並自訂錯誤訊息。但 ``pipefail`` 讓管線的非零狀態成為整條
管線的狀態，``set -e`` 於是就在**管線那一行**終止腳本 ⇒ 之後那段 rc 檢查與它
的訊息**永遠不會執行**。守衛看起來存在，實際上從來沒有守過。

重現::

    bash -c 'set -euo pipefail; (exit 7)|tee /dev/null; echo NOPE'   # 不印 NOPE、exit 7
    bash -c 'set -eu;          (exit 7)|tee /dev/null; echo NOPE'   # 印 NOPE

⚠️ 第二條說明**為什麼述詞必須同時要求 errexit 與 pipefail**：拿掉 pipefail 之後
同一段程式碼會醒過來，是合法的（而且是一層有用的後備）。只看 ``PIPESTATUS``
或只看 ``set -e`` 都會誤紅。

述詞（narrow，PIPESTATUS 專用）
------------------------------
一處 ``PIPESTATUS`` 讀取算違規，當且僅當**全部**成立：

1. 它在程式碼上，不在註解裡。
2. 在該行的位置，``errexit`` 與 ``pipefail`` **同時**生效。
3. 它前面最近的一個語句是一條**管線**（含未被引號包住的 ``|``，且不是 ``||``）。
4. 那條管線**沒有被保護**——不是 ``if`` / ``while`` / ``until`` / ``!`` 的條件，
   也沒有用 ``||`` / ``&&`` 接住。

第 3、4 條讓述詞保守：讀不到管線就不判違規，寧可漏也不誤紅（D-05e——誤紅才是
守衛被刪掉的原因）。

⛔ **不確定就不判**（owner 裁決）
--------------------------------
容器內沒有權威 shell parser（``bashlex`` / ``shfmt`` / ``shellcheck`` 都沒有），
所以碰到手刻 lexer 決定不了的構造，整個單元**拒絕判定並在報告裡點名**，而不是猜
（見 ``_HARD_CONSTRUCTS``）。

⚠️ 這是用**覆蓋面換正確性**：被拒判的單元不會有 finding。⛔ 「跳過」與「掃過且
乾淨」在輸出與 JSON 裡分得開（``skipped`` ／``scanned``），因為兩者混在一起正是
本條線要防的那個病。實際比例每次執行都會印，這裡不複製一份會漂的快照。
釘住它的是 ``test_skipped_is_reported_and_not_counted_as_clean``。

⚠️ **拒判也要有證據，跟 finding 一樣**。兩個方向都會出事，兩個方向都有測試釘住：
判得對的構造被列進拒判清單（過度拒判＝把真違規靜默丟掉）由
``test_shapes_that_must_stay_decidable`` 擋；prescan 必須先 ``_mask()`` 掉註解與
引號內文，否則字串裡的 ``<<<`` 會誤觸拒判，由
``test_hard_constructs_inside_comments_and_strings_do_not_refuse`` 擋。

母體
----
- git 追蹤的 ``*.sh``
- ``.github/workflows/*.yml|yaml`` 的 ``run:`` 區塊

⚠️ **GitHub Actions 的預設 shell 不是 ``bash -euo pipefail``**：不指定 ``shell:``
時是 ``bash -e {0}``（errexit 開、pipefail **關**）；寫了 ``shell: bash`` 才是
``bash --noprofile --norc -eo pipefail {0}``（兩者都開）。本工具照這個模型起始，
再套用區塊內實際出現的 ``set`` 語句。

Usage
-----
::

    python3 scripts/tools/lint/check_unreachable_pipestatus.py         # report
    python3 scripts/tools/lint/check_unreachable_pipestatus.py --ci    # exit 1 on violations
    python3 scripts/tools/lint/check_unreachable_pipestatus.py --json  # machine-readable

Exit codes
----------
- ``0`` — 掃過了。⚠️ **不等於沒有違規**：沒給 ``--ci`` 時 findings 照印、rc 仍是 0
  （報告模式不當閘門）。要「有違規就非零」必須給 ``--ci``。
- ``1`` — 有違規，**且**給了 ``--ci``
- ``2`` — **量不到**：不是 git repo、git 不可用、母體是空的、PyYAML 不可用、
  或**任一 workflow 檔 YAML 解析失敗**。⛔ 空母體絕不回 0——「工具失能」長得就像
  「零命中」（D-07d），必須大聲失敗。⚠️ workflow 解析失敗特別列出來，是因為它
  先前 ``except yaml.YAMLError: return []``：一處與 ``run:`` 無關的語法錯就讓整個
  檔案貢獻 0 個單元、零診斷，而母體被別的檔案撐著 ⇒ 工具照樣印「✅ 量了沒事」。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - 由 pre-commit 隔離 venv 觸發
    yaml = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PIPESTATUS_RE = re.compile(r"PIPESTATUS\[")
_WORKFLOW_DIR = Path(".github") / "workflows"

# heredoc 開頭：<<EOF / <<-EOF / <<'EOF' / <<"EOF"
_HEREDOC_RE = re.compile(r"<<-?\s*([\'\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


# ⛔ 「不確定就不判」的清單（owner 裁決）。容器內沒有權威 shell parser，碰到手刻
# lexer 決定不了的構造，整個單元**拒絕判定並在報告裡點名**，而不是猜。
#
# 每一條都要有實測依據（bash 為 ground truth）：
#   <<<        herestring 被 _HEREDOC_RE 當成 heredoc 開頭 ⇒ 之後整個檔案被跳過
#   case       `foo|bar)` 的模式交替與管線同形
#   function   關鍵字式函式定義對 `()` 為準的函式追蹤完全不可見
#   { } group  函式體內的命令群組會提早關掉巢狀計數
#
# ⚠️ 這是**用覆蓋面換正確性**：被跳過的單元不會有 finding，報告會說出有幾個、
# 為什麼。⛔ 「跳過」與「掃過且乾淨」在輸出裡必須分得開。
# ⛔ 這張清單**只收有實測重現的構造**——判得對的構造列進來就是過度拒判，會把真違規
# 靜默丟掉。`case "$x" in foo|bar)` 與函式外的 `cmd || { …; }` 兩個形狀 lexer 判得
# 對，由 `test_shapes_that_must_stay_decidable` 釘住它們不得被拒判。
_HARD_CONSTRUCTS = (
    # G1：`cat <<< var` 被 _HEREDOC_RE 當成 heredoc 開頭，終止詞永不出現 ⇒
    #     之後整個檔案被 lex 跳過（實測 bash rc=1、守衛 0 findings）。
    ("herestring", re.compile(r"<<<")),
    # G3：關鍵字式函式定義對以 `()` 為準的函式追蹤完全不可見
    #     （實測 bash rc=1、守衛 0 findings）。
    ("function-keyword", re.compile(r"(?:^|\s)function\s+[\w-]+")),
)

# G4 是**組合**才會壞：命令群組 `{ … }` 出現在 `name() { … }` 函式體內時，
# 群組的 `}` 會提早關掉巢狀計數（實測 bash rc=1、守衛 0 findings）。
# 函式外的 `|| { …; }` 是最常見的慣用法且 lexer 判得對，所以不能一起拒判。
_FUNC_DEF_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*\(\)", re.M)
# ⚠️ 只認**群組的開頭**，不認函式自己的大括號：把 `^\s*\}$` 也算進來的話，每一個
# `name() { … }` 函式的結尾 `}` 都會命中 ⇒ 所有具名函式的檔案全被拒判。
# 群組開頭的形狀是「行首的 `{ `」或「`;` / `&&` / `||` 之後的 `{ `」；
# `name() {` 的 `{` 前面是 `)`，兩者都不match。
_BRACE_GROUP_RE = re.compile(r"(?:^\s*|[;&|]\s*)\{\s", re.M)


# **裸的**多行 `( … )` 子 shell 與 `{ … }` 命令群組：lexer 只追 `$(` 與反引號、不追
# 它們的巢狀，於是群組內每一物理行都被 flush 成獨立語句、真正的管線不再是 `prev`
# ⇒ **既沒判對也沒被拒判，直接報「乾淨」**（bash 實測兩者皆 rc=1）。那是「判不了的
# 都會被拒判」這個安全性宣稱的反例，所以補進拒判。
# ⚠️ **只認自成一行的「開頭」**——連結尾 `)` / `}` 一起認的話，`name() { … }` 函式
# 自己的結尾大括號會命中，就變成上面 `_BRACE_GROUP_RE` 那個過度拒判。
# 釘住：`test_bare_multiline_groups_are_refused`（漏判方向）與
# `test_shapes_that_must_stay_decidable`（過度拒判方向）。
_BARE_GROUP_RE = re.compile(r"^\s*[({]\s*$", re.M)


def _strip_heredocs(body: str) -> str:
    """把 heredoc **內文**換成空行，行號不變。

    ⛔ `lex()` 本來就把 heredoc 內文當成非程式碼，prescan 卻沒有 —— 於是內文裡的
    `{`、`function foo`、`<<<` 會誤觸拒判，**把真違規靜默丟掉**。
    釘住：`test_heredoc_body_does_not_trigger_refusal`。
    """
    out: list[str] = []
    pending: list[str] = []
    open_terms: list[str] = []
    for line in body.split("\n"):
        if open_terms:
            if line.strip() == open_terms[0] or line.lstrip("\t").strip() == open_terms[0]:
                open_terms.pop(0)
                out.append(line)
            else:
                out.append("")
            continue
        for m in _HEREDOC_RE.finditer(line):
            pending.append(m.group(2))
        out.append(line)
        if pending:
            open_terms.extend(pending)
            pending = []
    return "\n".join(out)


def _mask(body: str) -> str:
    """把 heredoc 內文、註解與引號內文換成空白，只留下真正的程式碼字元給 prescan。

    ⛔ **拒判用的證據必須跟判定用的一樣乾淨**：prescan 若對原始文字跑 regex，字串／
    註解／heredoc 內文裡的構造都會誤觸拒判，而拒判會把真違規靜默丟掉。
    釘住：`test_hard_constructs_inside_comments_and_strings_do_not_refuse` 與
    `test_heredoc_body_does_not_trigger_refusal`。
    """
    body = _strip_heredocs(body)
    out = []
    quote = None
    i = 0
    prev_ws = True
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(body):
                out.append("  ")
                i += 2
                continue
            if ch == quote:
                quote = None
                out.append(" ")
            else:
                out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
            prev_ws = False
            i += 1
            continue
        if ch == "#" and prev_ws:
            while i < len(body) and body[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        prev_ws = ch.isspace()
        i += 1
    return "".join(out)


_FUNC_OPEN_RE = re.compile(r"^[ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*\(\)[ \t]*\{", re.M)


def _func_spans(body: str) -> list[tuple[int, int]] | None:
    """回傳每個 `name() { … }` 函式體的 (起, 迄) 位移；配對不起來回 None（＝不確定）。"""
    spans: list[tuple[int, int]] = []
    for m in _FUNC_OPEN_RE.finditer(body):
        depth = 0
        i = m.end() - 1
        while i < len(body):
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
                if depth == 0:
                    spans.append((m.end(), i))
                    break
            i += 1
        else:
            return None
    return spans


def hard_constructs(lines: list[tuple[int, str]]) -> list[str]:
    """回傳這個單元裡出現的、lexer 決定不了的構造名稱（去重、排序）。"""
    body = _mask("\n".join(raw for _, raw in lines))
    found = {name for name, rx in _HARD_CONSTRUCTS if rx.search(body)}
    # ⛔ **包含關係，不是全檔存在性**：函式定義與命令群組各自出現在檔案的不同地方是
    # 常見慣用法（`cmd || { …; }` 在函式外），lexer 判得對。只有群組真的落在函式體
    # **內**才會提早關掉巢狀計數。配對不起來就當不確定、拒判。
    # 釘住：`test_function_and_unrelated_brace_group_stay_decidable`。
    spans = _func_spans(body)
    if spans is None:
        found.add("brace-group-in-function")
    elif spans and any(
        any(a < m.start() < b for a, b in spans) for m in _BRACE_GROUP_RE.finditer(body)
    ):
        found.add("brace-group-in-function")
    if _BARE_GROUP_RE.search(body):
        found.add("bare-group")
    return sorted(found)


class Stmt:
    """一個邏輯語句（已依 ; && || 換行切開，並知道自己有沒有 top-level 管線）。"""

    __slots__ = ("line", "text", "top_pipe", "sub_pipe", "sep_before", "in_func")

    def __init__(self, line, text, top_pipe, sub_pipe, sep_before, in_func):
        self.line = line
        self.text = text
        self.top_pipe = top_pipe      # 深度 0、不在引號內的 `|`
        self.sub_pipe = sub_pipe      # 只出現在 $(...) / `...` 內的 `|`
        self.sep_before = sep_before  # 前一個分隔符：'\n' ';' '&&' '||' '' (檔首)
        self.in_func = in_func


def lex(lines: list[tuple[int, str]]) -> list[Stmt]:
    """把 (行號, 原始行) 串列切成邏輯語句。

    ⛔ 這一版刻意處理三件手刻逐行掃描做不到、而且已實測會出錯的事（各附重現）：

    1. **heredoc 內文不是程式碼**。``true | cat <<'DOC'`` 之後的內文若含
       ``${PIPESTATUS[0]}`` 字樣，逐行掃描會把它當成一次讀取而**誤紅**。
    2. **``$(...)`` / 反引號裡的管線不是本語句的 top-level 管線**。
       ``echo $(false | true) end`` 的管線失敗**不會**觸發 errexit（實測
       rc=0、後續行照跑），把它當 top-level 會**誤紅**。
    3. **``;`` 也是語句分隔符**。``false | true; RC="${PIPESTATUS[0]}"``
       在同一物理行上，逐行掃描看不到，於是**漏抓**（實測 rc=1、下一行不可達）。
    """
    stmts: list[Stmt] = []
    quote = None
    depth = 0                     # $( ) / ` ` 巢狀深度
    heredocs: list[str] = []      # 待關閉的 heredoc 終止詞
    pending_heredoc: list[str] = []
    func_depth = 0                # { } 巢狀，用來粗判是否在函式體內
    # ⛔ `case` 的模式交替 `foo|bar)` 與管線同形。不追蹤它會把**合法且可達**的
    # PIPESTATUS 讀取報成違規（誤紅是守衛被刪掉的原因）。
    # 釘住：`test_case_pattern_alternation_is_not_a_pipeline`。
    case_depth = 0                # 巢狀 case … esac
    in_pattern = False            # 目前在 case 的模式位置（`|` 是交替）
    buf, buf_line = "", None
    top_pipe = sub_pipe = False
    sep = ""
    next_sep = "\n"

    def flush(new_sep):
        nonlocal buf, buf_line, top_pipe, sub_pipe, sep, case_depth, in_pattern
        text = buf.strip()
        if text:
            stmts.append(Stmt(buf_line or 0, text, top_pipe, sub_pipe, sep, func_depth > 0))
            if re.match(r"^case\b.*\bin$", text):
                case_depth += 1
                in_pattern = True
            elif text == "esac" and case_depth > 0:
                case_depth -= 1
                in_pattern = False
        buf, buf_line, top_pipe, sub_pipe = "", None, False, False
        sep = new_sep

    for lineno, raw in lines:
        # --- heredoc 內文：整行跳過，只找終止詞 ---
        if heredocs:
            if raw.strip() == heredocs[0] or raw.lstrip("\t").strip() == heredocs[0]:
                heredocs.pop(0)
            continue
        i = 0
        line_had_content = False
        while i < len(raw):
            ch = raw[i]
            if quote:
                buf += ch
                if ch == "\\" and quote == '"' and i + 1 < len(raw):
                    buf += raw[i + 1]
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                buf += ch
                if buf_line is None:
                    buf_line = lineno
                i += 1
                continue
            if ch == "\\" and i + 1 < len(raw):
                buf += raw[i : i + 2]
                i += 2
                continue
            if ch == "#" and (i == 0 or raw[i - 1].isspace()):
                break  # 行內註解
            # --- 巢狀 ---
            # ⛔ 算術命令 `(( … ))` 裡的 `|` 是**位元或**，不是管線。lexer 原本只追
            # `$(` 與反引號 ⇒ `(( a = 1 | 2 ))` 被讀成 top-level 管線而誤紅。
            # 深度 +2，讓收尾的兩個 `)` 各自扣回來。
            # 釘住：`test_arithmetic_command_bitwise_or_is_not_a_pipeline`。
            if raw.startswith("((", i) and depth == 0 and not buf.rstrip().endswith("$"):
                depth += 2
                buf += "(("
                if buf_line is None:
                    buf_line = lineno
                i += 2
                continue
            if raw.startswith("$(", i):
                depth += 1
                buf += "$("
                if buf_line is None:
                    buf_line = lineno
                i += 2
                continue
            if ch == "`":
                depth += 1 if depth == 0 else -1
                buf += ch
                i += 1
                continue
            if ch == ")" and depth > 0:
                depth -= 1
                buf += ch
                i += 1
                continue
            # --- heredoc 開頭 ---
            m = _HEREDOC_RE.match(raw, i)
            if m:
                pending_heredoc.append(m.group(2))
                buf += m.group(0)
                if buf_line is None:
                    buf_line = lineno
                i = m.end()
                continue
            # --- 分隔符（只在深度 0 有效）---
            if depth == 0 and raw.startswith(";;", i) and case_depth > 0:
                flush(";")
                in_pattern = True
                i += 2
                continue
            if depth == 0 and ch == ";":
                flush(";")
                i += 1
                continue
            if depth == 0 and raw.startswith("&&", i):
                flush("&&")
                i += 2
                continue
            if depth == 0 and raw.startswith("||", i):
                flush("||")
                i += 2
                continue
            if ch == ")" and depth == 0 and in_pattern:
                # 模式結束、回到命令位置。⚠️ 這個 reset 是必要的：少了它，整個 case
                # 區塊之後都被當成模式位置，分支**體內**的真管線會被漏掉。
                # 釘住：`test_pipeline_inside_a_case_branch_is_still_caught`。
                in_pattern = False
            if ch == "|":
                if depth == 0 and not in_pattern:
                    top_pipe = True
                elif depth != 0:
                    sub_pipe = True
                buf += ch
                if buf_line is None:
                    buf_line = lineno
                i += 1
                continue
            if ch == "{" and depth == 0 and buf.strip().endswith("()"):
                func_depth += 1
            if ch == "}" and depth == 0 and func_depth > 0 and not buf.strip():
                func_depth -= 1
            buf += ch
            if not ch.isspace():
                line_had_content = True
                if buf_line is None:
                    buf_line = lineno
            i += 1
        # 行尾
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1] + " "        # 續行
        elif quote is None and depth == 0:
            flush("\n")
        if pending_heredoc:
            heredocs.extend(pending_heredoc)
            pending_heredoc = []
        if not line_had_content and not buf.strip():
            continue
    flush("\n")
    return stmts


def apply_set(code: str, errexit: bool, pipefail: bool) -> tuple[bool, bool]:
    """把一行 ``set ...`` 套用到 (errexit, pipefail) 狀態上。"""
    toks = code.strip().split()
    if not toks or toks[0] != "set":
        return errexit, pipefail
    return _apply_flags(toks[1:], errexit, pipefail)


def _apply_flags(toks: list[str], errexit: bool, pipefail: bool) -> tuple[bool, bool]:
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-o", "+o"):
            if i + 1 < len(toks) and toks[i + 1] == "pipefail":
                pipefail = t == "-o"
                i += 2
                continue
            i += 2
            continue
        if t.startswith("-") and not t.startswith("--"):
            letters = t[1:]
            if "e" in letters:
                errexit = True
            if letters.endswith("o") and i + 1 < len(toks) and toks[i + 1] == "pipefail":
                pipefail = True
                i += 2
                continue
        elif t.startswith("+"):
            letters = t[1:]
            if "e" in letters:
                errexit = False
            if letters.endswith("o") and i + 1 < len(toks) and toks[i + 1] == "pipefail":
                pipefail = False
                i += 2
                continue
        i += 1
    return errexit, pipefail


def shebang_flags(lines: list[tuple[int, str]]) -> tuple[bool, bool]:
    """``#!/bin/bash -e`` 這種內嵌旗標也會生效（實測 rc=1、守衛區段不可達）。"""
    if not lines:
        return False, False
    first = lines[0][1]
    if not first.startswith("#!"):
        return False, False
    toks = first[2:].split()
    return _apply_flags(toks[1:], False, False) if len(toks) > 1 else (False, False)


_GUARD_PREFIX = re.compile(r"^\s*(if|while|until|elif)\b|^\s*!\s")


_COND_END = re.compile(r"^\s*(then|do)\b")


def is_protected(stmt: Stmt) -> bool:
    """這條語句自己就以 ``if`` / ``while`` / ``until`` / ``elif`` / ``!`` 開頭。"""
    return bool(_GUARD_PREFIX.search(stmt.text))


def scan_unit(lines: list[tuple[int, str]], errexit: bool, pipefail: bool) -> list[dict]:
    """掃一個 shell 單元。``lines`` 是 (行號, 原始行) 串列。

    ⛔ 碰到 ``_HARD_CONSTRUCTS`` 一律**不判定**（回空），由呼叫端記成 skipped。
    """
    if hard_constructs(lines):
        return []
    se, sp = shebang_flags(lines)
    errexit, pipefail = errexit or se, pipefail or sp

    stmts = lex(lines)

    # 函式體的選項狀態：用「該函式最早一次 top-level 呼叫」當生效點。
    # 這是為了抓「函式定義在 set 之前、呼叫在之後」那一類（實測會漏）。
    func_names = set()
    for st in stmts:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", st.text)
        if m:
            func_names.add(m.group(1))
    # ⛔ 函式體要對**每一個**呼叫點的狀態求值，取「存在一個呼叫點使讀取不可達」。
    # 只看最早那次會漏掉「先在 set +e 下呼叫、之後在 set -euo pipefail 下再呼叫」。
    # 釘住：`test_second_call_site_under_stricter_state_is_caught`。
    # ⛔ 存的是**每個呼叫點的整組狀態**，不是兩個旗標各自 OR —— 各自 OR 會把
    # 「呼叫甲只有 errexit、呼叫乙只有 pipefail」合成一個從未存在的 (True, True)，
    # 於是誤紅一段兩次呼叫都真的執行到的程式碼。
    # 釘住：`test_flags_from_different_call_sites_are_not_merged`。
    state_at: dict[str, set[tuple[bool, bool]]] = {}
    e, pf = errexit, pipefail
    for st in stmts:
        if st.text.split()[:1] == ["set"]:
            e, pf = apply_set(st.text, e, pf)
        head = st.text.split()[0] if st.text.split() else ""
        if head in func_names and not st.in_func:
            state_at.setdefault(head, set()).add((e, pf))

    # ⛔ 述詞不是「**前一個**語句是管線」，而是「**在**一條沒被接住的 top-level 管線
    # **之後**」——errexit 在管線那一行就終止腳本，後面**整段**都不可達，中間隔幾個
    # `echo` 不會讓它復活。只看前一個語句會讓一個無害的中間語句就打穿整支守衛。
    # 釘住：`test_statement_between_pipeline_and_read_is_still_unreachable`。
    violations: list[dict] = []
    e, pf = errexit, pipefail
    cur_func: str | None = None
    dead: dict[str | None, Stmt | None] = {}   # 每個範圍各自的「之後不可達」標記
    # ⛔ `if` / `while` / `until` 的**條件串列整段**免除 errexit，不是只有第一個命令。
    # `if [ -n "$X" ] && foo | bar; then` 的那條管線是被保護的（bash 實測 rc=0）。
    # 釘住：`test_pipeline_later_in_an_if_condition_list_is_protected`。
    in_cond = False
    for idx, st in enumerate(stmts):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", st.text)
        if m:
            # ⛔ 只重置**這個函式自己**的範圍。原本無條件把 dead 清掉，於是一個
            # 出現在致命管線**之後**（因此自己也不可達）的函式定義就能讓守衛忘記
            # 前面已經死掉 ⇒ 真違規被報成 scanned + clean（最糟的一類）。
            # 釘住：`test_function_definition_after_a_fatal_pipeline_does_not_revive`。
            cur_func = m.group(1)
            dead[cur_func] = None
        elif not st.in_func:
            cur_func = None
        scope = cur_func if st.in_func else None
        if _COND_END.match(st.text):
            in_cond = False
        if st.text.split()[:1] == ["set"]:
            e, pf = apply_set(st.text, e, pf)
            continue
        # 函式體內改用「呼叫點」的狀態；查不到呼叫就保守跳過
        if st.in_func and cur_func:
            if cur_func not in state_at:
                continue
            # 「存在一個呼叫點使讀取不可達」＝ 有任一組同時開著 errexit 與 pipefail
            ce = cpf = any(x and y for x, y in state_at[cur_func])
        else:
            ce, cpf = e, pf

        d = dead.get(scope)
        if _PIPESTATUS_RE.search(st.text) and ce and cpf and d is not None:
            violations.append(
                {"line": st.line, "code": st.text[:120], "pipeline": d.text[:120]}
            )

        if st.top_pipe and not is_protected(st) and not in_cond:
            # 被 `&&` / `||` 接住的管線不會觸發 errexit —— 看**下一個**語句的前導分隔符
            nxt = stmts[idx + 1] if idx + 1 < len(stmts) else None
            if nxt is None or nxt.sep_before not in ("&&", "||"):
                dead[scope] = st
        if is_protected(st):
            in_cond = True
    return violations


def iter_shell_units(repo: Path, parse_errors: list[str] | None = None) -> list[dict]:
    """母體：git 追蹤的 *.sh + workflow 的 run: 區塊。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "*.sh", ".github/workflows/*.yml", ".github/workflows/*.yaml"],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        raise RuntimeError(f"git ls-files failed: {exc}") from exc

    paths = [p for p in out.decode("utf-8").split("\0") if p]
    units: list[dict] = []
    parse_errors = parse_errors if parse_errors is not None else []
    for rel in paths:
        fp = repo / rel
        try:
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if rel.endswith(".sh"):
            units.append(
                {
                    "path": rel,
                    "kind": "script",
                    "errexit": False,
                    "pipefail": False,
                    "lines": list(enumerate(text.splitlines(), start=1)),
                }
            )
        else:
            # ⛔ 解析錯**不再中止整輪**。先前一有 YAML 錯就
            # 讓整個 run 以 rc 2 結束、stdout 全空 ⇒ 其他檔案裡**已經找到的真違規**
            # 一併被吞掉。那是把「靜默假綠」換成「全面停播」，而不是換成
            # 「報告我找到的 + 標記我量不到的」。現在逐檔收集，兩者都印，rc 仍是 2。
            try:
                units.extend(_workflow_run_units(rel, text))
            except WorkflowParseError as exc:
                parse_errors.append(str(exc))
    return units


class WorkflowParseError(RuntimeError):
    """⛔ workflow 解析不出來 —— 這是「量不到」，不是「量了沒事」。"""


def _shell_is_pipefail(shell: object) -> bool:
    """GitHub 的 shell 解析：``bash`` ⇒ ``bash --noprofile --norc -eo pipefail {0}``。

    不指定 ``shell:`` 時是 ``bash -e {0}``（errexit 開、pipefail **關**）。
    """
    return isinstance(shell, str) and shell.strip() == "bash"


def _scalar(node: object) -> object:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _get(node: object, key: str):
    """從 MappingNode 取一個 key 的 value node。"""
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _defaults_shell(node: object) -> object:
    return _scalar(_get(_get(_get(node, "defaults"), "run"), "shell"))


def _workflow_run_units(rel: str, text: str) -> list[dict]:
    """把 workflow 的 ``run:`` 區塊切出來，並依 YAML 解析 shell 的繼承。

    ⛔ **YAML 解析失敗一律拋 WorkflowParseError，不回空**。先前它
    ``except yaml.YAMLError: return []``：一處與 ``run:`` 完全無關的語法錯就讓
    **整個檔案**貢獻 0 個單元、零診斷，工具照樣印「✅ 量了沒事」——本條線的核心禁忌
    出現在自己的工具裡。釘住：``test_yaml_parse_error_is_rc2_not_a_clean_pass``
    與 ``test_parse_error_does_not_swallow_findings_from_other_files``。

    ⚠️ 行號取自 YAML 節點的 ``start_mark``，不是拿首行去全檔比對——本 repo 有大量
    run step 首行彼此相同（``set -euo pipefail`` 就是其一），比對會誤植。釘住：
    ``test_run_block_line_number_comes_from_the_yaml_node``。
    """
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise WorkflowParseError(f"{rel}: {exc}") from exc
    if not isinstance(root, yaml.MappingNode):
        return []

    wf_shell = _defaults_shell(root)
    units: list[dict] = []
    jobs = _get(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return []

    for _, job in jobs.value:
        job_shell = _defaults_shell(job)
        steps = _get(job, "steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for step in steps.value:
            run = _get(step, "run")
            if not isinstance(run, yaml.ScalarNode) or not isinstance(run.value, str):
                continue
            step_shell = _scalar(_get(step, "shell"))
            shell = step_shell if step_shell is not None else (
                job_shell if job_shell is not None else wf_shell
            )
            # literal block（`|`）的內容從標記行的下一行開始，與實體行一一對應；
            # plain scalar 的內容就在標記行上。
            #
            # ⛔ folded（`>`）**不行**：YAML 折疊會把連續非空行併成一行、空行才變成
            # 換行，於是 `value.splitlines()` 與實體行不再一一對應，`base + i` 會默默
            # 漂掉。行號報錯對一支 lint 來說就是壞掉，所以**拒判**而不是猜。
            # 釘住：`test_folded_scalar_run_block_is_refused` /
            # `test_literal_block_is_still_scanned`。
            folded = run.style == ">"
            base = run.start_mark.line + (2 if run.style in ("|", ">") else 1)
            units.append(
                {
                    "path": f"{rel}:{run.start_mark.line + 1}",
                    "kind": "workflow-run",
                    "errexit": True,          # GitHub 預設就是 bash -e
                    "pipefail": _shell_is_pipefail(shell),
                    "folded": folded,
                    "lines": [(base + i, ln) for i, ln in enumerate(run.value.splitlines())],
                }
            )
    return units


def main(argv: list[str] | None = None) -> int:
    # ⛔ try_utf8_stdout() 要在 argparse 之前：`--help` 裡的 CJK 在 legacy Windows
    # console（cp950/cp936）會在 argparse 印出來之前就 UnicodeEncodeError。
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=i18n_text(
            "找出 `set -e` + `pipefail` 之下不可達的 PIPESTATUS 讀取——"
            "管線失敗時腳本就在管線那一行終止，之後的 rc 檢查與它的訊息永遠不會執行。",
            "Find PIPESTATUS reads that are unreachable under `set -e` + "
            "`pipefail`: a failing pipeline terminates the script at the "
            "pipeline itself, so the rc check after it never runs.",
        ),
    )
    ap.add_argument("--ci", action="store_true",
                    help=i18n_text("有違規時 exit 1", "exit 1 when violations are found"))
    ap.add_argument("--json", action="store_true",
                    help=i18n_text("輸出 JSON", "emit JSON"))
    ap.add_argument("--repo", default=str(_REPO_ROOT),
                    help=i18n_text("要掃描的 repo 根目錄", "repository root to scan"))
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if yaml is None:
        print(
            "[unreachable-pipestatus] ⛔ 量不到：PyYAML 不可用，workflow run 區塊無法解析。"
            "（pre-commit 需要 additional_dependencies: ['pyyaml']）",
            file=sys.stderr,
        )
        return EXIT_CALLER_ERROR

    if not (repo / ".git").exists():
        print(f"[unreachable-pipestatus] ⛔ 量不到：{repo} 不是 git repo", file=sys.stderr)
        return 2

    parse_errors: list[str] = []
    try:
        units = iter_shell_units(repo, parse_errors)
    except RuntimeError as exc:
        print(f"[unreachable-pipestatus] ⛔ 量不到：{exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if not units:
        print(
            "[unreachable-pipestatus] ⛔ 量不到：母體是空的（0 個 shell 單元）。"
            "工具失能與零命中長得一樣，所以這裡不回 0。",
            file=sys.stderr,
        )
        return 2

    findings: list[dict] = []
    skipped: list[dict] = []
    for u in units:
        hard = hard_constructs(u["lines"])
        if u.get("folded"):
            hard = sorted(set(hard) | {"folded-scalar"})
        if hard:
            # ⛔ 拒絕判定 ≠ 判定為乾淨。記下來、印出來、進 JSON。
            skipped.append({"path": u["path"], "reason": hard})
            continue
        for v in scan_unit(u["lines"], u["errexit"], u["pipefail"]):
            findings.append({"path": u["path"], "kind": u["kind"], **v})

    if args.json:
        print(json.dumps(
            {"units": len(units), "scanned": len(units) - len(skipped),
             "skipped": skipped, "parse_errors": parse_errors, "findings": findings},
            ensure_ascii=False, indent=2))
    else:
        print(f"[unreachable-pipestatus] 母體：{len(units)} 個 shell 單元 "
              f"（{sum(1 for u in units if u['kind'] == 'script')} scripts / "
              f"{sum(1 for u in units if u['kind'] == 'workflow-run')} workflow run blocks）")
        if skipped:
            from collections import Counter
            why = Counter(r for sk in skipped for r in sk["reason"])
            print(f"[unreachable-pipestatus] ⚠️ 其中 {len(skipped)} 個**拒絕判定**"
                  f"（lexer 決定不了的構造：{', '.join(f'{k}×{v}' for k, v in why.most_common())}）"
                  f" —— 這些單元不會有 finding，⛔「跳過」不等於「乾淨」。")
        if findings:
            for f in findings:
                print(f"  ✗ {f['path']}:{f['line']} — PIPESTATUS 讀取不可達"
                      f"（errexit+pipefail 之下前一條管線失敗就終止）")
                print(f"      pipeline: {f['pipeline']}")
                print(f"      read:     {f['code']}")
        else:
            print(f"[unreachable-pipestatus] ✅ 量了沒事："
                  f"實際掃過的 {len(units) - len(skipped)} 個單元裡沒有不可達的 PIPESTATUS 讀取")

    # ⛔ 解析失敗是「量不到」，優先於「量了有事／沒事」：即使上面已經印出真違規，
    # 也要讓呼叫端知道**有一整個檔案沒被掃到**。findings 照印，不吞。
    if parse_errors:
        for err in parse_errors:
            print(f"[unreachable-pipestatus] ⛔ 量不到：workflow 解析失敗，"
                  f"整個檔案沒有被掃到 —— {err}", file=sys.stderr)
        print(f"[unreachable-pipestatus] ⛔ {len(parse_errors)} 個 workflow 檔解析失敗 "
              f"⇒ 上面的結果**不完整**（已找到的 {len(findings)} 條照常列出）。",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    if findings and args.ci:
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
