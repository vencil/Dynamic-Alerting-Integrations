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
- ``0`` — 掃過了，沒有違規（**量了沒事**）
- ``1`` — 有違規（``--ci``）
- ``2`` — **量不到**：不是 git repo、git 不可用、或母體是空的。⛔ 空母體絕不
  回 0——「工具失能」長得就像「零命中」（D-07d），必須大聲失敗。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - 由 pre-commit 隔離 venv 觸發
    yaml = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PIPESTATUS_RE = re.compile(r"PIPESTATUS\[")
_WORKFLOW_DIR = Path(".github") / "workflows"

# heredoc 開頭：<<EOF / <<-EOF / <<'EOF' / <<"EOF"
_HEREDOC_RE = re.compile(r"<<-?\s*([\'\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


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

    ⛔ 這一版刻意處理三件手刻逐行掃描做不到、而且已實測會出錯的事
    （TRK-381 第 2 輪盲審，三條各附重現）：

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
    buf, buf_line = "", None
    top_pipe = sub_pipe = False
    sep = ""
    next_sep = "\n"

    def flush(new_sep):
        nonlocal buf, buf_line, top_pipe, sub_pipe, sep
        if buf.strip():
            stmts.append(Stmt(buf_line or 0, buf.strip(), top_pipe, sub_pipe, sep, func_depth > 0))
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
            if ch == "|":
                if depth == 0:
                    top_pipe = True
                else:
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


def is_protected(stmt: Stmt) -> bool:
    """管線是否被 if/while/until/! 條件或 ``||`` / ``&&`` 接住。

    ⚠️ ``sep_before`` 也算：``pipeline && next`` 之後的語句，其前導分隔符就是
    ``&&`` ⇒ 那條管線的失敗被接住了，errexit 不觸發。
    """
    return bool(_GUARD_PREFIX.search(stmt.text))


def scan_unit(lines: list[tuple[int, str]], errexit: bool, pipefail: bool) -> list[dict]:
    """掃一個 shell 單元。``lines`` 是 (行號, 原始行) 串列。"""
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
    state_at: dict[str, tuple[bool, bool]] = {}
    e, pf = errexit, pipefail
    for st in stmts:
        if st.text.split()[:1] == ["set"]:
            e, pf = apply_set(st.text, e, pf)
        head = st.text.split()[0] if st.text.split() else ""
        if head in func_names and not st.in_func and head not in state_at:
            state_at[head] = (e, pf)

    violations: list[dict] = []
    e, pf = errexit, pipefail
    cur_func: str | None = None
    prev: Stmt | None = None
    for st in stmts:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)", st.text)
        if m:
            cur_func = m.group(1)
        if st.text.split()[:1] == ["set"]:
            e, pf = apply_set(st.text, e, pf)
            prev = st
            continue
        # 函式體內改用「呼叫點」的狀態；查不到呼叫就保守跳過
        if st.in_func and cur_func:
            if cur_func not in state_at:
                prev = st
                continue
            ce, cpf = state_at[cur_func]
        else:
            ce, cpf = e, pf
        if _PIPESTATUS_RE.search(st.text):
            if (
                ce
                and cpf
                and prev is not None
                and prev.top_pipe          # ⛔ 只認 top-level 管線，$(...) 內的不算
                and not is_protected(prev)
                and st.sep_before not in ("&&", "||")   # 被 &&/|| 接住就不會終止
            ):
                violations.append(
                    {"line": st.line, "code": st.text[:120], "pipeline": prev.text[:120]}
                )
        prev = st
    return violations


def iter_shell_units(repo: Path) -> list[dict]:
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
            units.extend(_workflow_run_units(rel, text))
    return units


def _shell_is_pipefail(shell: object) -> bool:
    """GitHub 的 shell 解析：``bash`` ⇒ ``bash --noprofile --norc -eo pipefail {0}``。

    不指定 ``shell:`` 時是 ``bash -e {0}``（errexit 開、pipefail **關**）。
    """
    return isinstance(shell, str) and shell.strip() == "bash"


def _workflow_run_units(rel: str, text: str) -> list[dict]:
    """把 workflow 的 ``run:`` 區塊切出來，並依 YAML 解析 shell 的繼承。

    ⚠️ 先前這裡是往上掃「同一 step 內有沒有一行 ``shell: bash``」的字串比對，
    實測三種形狀會判錯（TRK-381 第 2 輪盲審）：行尾帶註解、``shell:`` 與
    ``run:`` 之間有空行、以及 **job / workflow 層的 ``defaults.run.shell``**
    —— 後者與 step 層等效，卻整個看不見。現在改由 YAML parser 回答。
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []

    wf_shell = (((doc.get("defaults") or {}).get("run") or {}).get("shell"))
    units: list[dict] = []
    lines = text.splitlines()

    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        job_shell = (((job.get("defaults") or {}).get("run") or {}).get("shell"))
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or "run" not in step:
                continue
            body = step.get("run")
            if not isinstance(body, str):
                continue
            shell = step.get("shell", job_shell if job_shell is not None else wf_shell)
            start = _locate_block(lines, body)
            units.append(
                {
                    "path": f"{rel}:{start}" if start else rel,
                    "kind": "workflow-run",
                    "errexit": True,          # GitHub 預設就是 bash -e
                    "pipefail": _shell_is_pipefail(shell),
                    "lines": [(start + i if start else i + 1, ln)
                              for i, ln in enumerate(body.splitlines())],
                }
            )
    return units


def _locate_block(lines: list[str], body: str) -> int:
    """在原始檔裡找 run 區塊第一行的行號，讓回報指得回去。"""
    first = next((l for l in body.splitlines() if l.strip()), None)
    if first is None:
        return 0
    needle = first.strip()
    for idx, ln in enumerate(lines, start=1):
        if ln.strip() == needle:
            return idx
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true", help="有違規時 exit 1")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--repo", default=str(_REPO_ROOT))
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if yaml is None:
        print(
            "[unreachable-pipestatus] ⛔ 量不到：PyYAML 不可用，workflow run 區塊無法解析。"
            "（pre-commit 需要 additional_dependencies: ['pyyaml']）",
            file=sys.stderr,
        )
        return 2

    if not (repo / ".git").exists():
        print(f"[unreachable-pipestatus] ⛔ 量不到：{repo} 不是 git repo", file=sys.stderr)
        return 2

    try:
        units = iter_shell_units(repo)
    except RuntimeError as exc:
        print(f"[unreachable-pipestatus] ⛔ 量不到：{exc}", file=sys.stderr)
        return 2

    if not units:
        print(
            "[unreachable-pipestatus] ⛔ 量不到：母體是空的（0 個 shell 單元）。"
            "工具失能與零命中長得一樣，所以這裡不回 0。",
            file=sys.stderr,
        )
        return 2

    findings: list[dict] = []
    for u in units:
        for v in scan_unit(u["lines"], u["errexit"], u["pipefail"]):
            findings.append({"path": u["path"], "kind": u["kind"], **v})

    if args.json:
        print(json.dumps({"units": len(units), "findings": findings}, ensure_ascii=False, indent=2))
    else:
        print(f"[unreachable-pipestatus] 母體：{len(units)} 個 shell 單元 "
              f"（{sum(1 for u in units if u['kind'] == 'script')} scripts / "
              f"{sum(1 for u in units if u['kind'] == 'workflow-run')} workflow run blocks）")
        if findings:
            for f in findings:
                print(f"  ✗ {f['path']}:{f['line']} — PIPESTATUS 讀取不可達"
                      f"（errexit+pipefail 之下前一條管線失敗就終止）")
                print(f"      pipeline: {f['pipeline']}")
                print(f"      read:     {f['code']}")
        else:
            print("[unreachable-pipestatus] ✅ 量了沒事：沒有不可達的 PIPESTATUS 讀取")

    if findings and args.ci:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
