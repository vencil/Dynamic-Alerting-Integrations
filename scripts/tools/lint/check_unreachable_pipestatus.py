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

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PIPESTATUS_RE = re.compile(r"PIPESTATUS\[")
_WORKFLOW_DIR = Path(".github") / "workflows"


# --------------------------------------------------------------------------
# shell 文字處理
# --------------------------------------------------------------------------
def strip_comment(line: str) -> str:
    """去掉未被引號包住的 ``#`` 之後的內容。

    ``#`` 只有在行首或前面是空白時才起始註解（``foo#bar`` 不是註解），
    這與 bash 的 token 規則一致。
    """
    out = []
    quote: str | None = None
    prev_ws = True
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            prev_ws = False
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
            prev_ws = False
        elif ch == "\\" and i + 1 < len(line):
            out.append(ch)
            out.append(line[i + 1])
            i += 2
            prev_ws = False
            continue
        elif ch == "#" and prev_ws:
            break
        else:
            out.append(ch)
            prev_ws = ch.isspace()
        i += 1
    return "".join(out)


def has_bare_pipe(code: str) -> bool:
    """該段程式碼是否含有未被引號包住、且不是 ``||`` 的管線符號。"""
    quote: str | None = None
    i = 0
    while i < len(code):
        ch = code[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(code):
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\\" and i + 1 < len(code):
            i += 2
            continue
        elif ch == "|":
            if i + 1 < len(code) and code[i + 1] == "|":
                i += 2
                continue
            if i > 0 and code[i - 1] == "|":
                i += 1
                continue
            return True
        i += 1
    return False


def apply_set(code: str, errexit: bool, pipefail: bool) -> tuple[bool, bool]:
    """把一行 ``set ...`` 套用到 (errexit, pipefail) 狀態上。"""
    toks = code.strip().split()
    if not toks or toks[0] != "set":
        return errexit, pipefail
    i = 1
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
            # 例如 -euo pipefail：字母旗標可與 o 連寫
            letters = t[1:]
            if "e" in letters:
                errexit = True
            if letters.endswith("o") and i + 1 < len(toks):
                if toks[i + 1] == "pipefail":
                    pipefail = True
                    i += 2
                    continue
        elif t.startswith("+"):
            letters = t[1:]
            if "e" in letters:
                errexit = False
            if letters.endswith("o") and i + 1 < len(toks):
                if toks[i + 1] == "pipefail":
                    pipefail = False
                    i += 2
                    continue
        i += 1
    return errexit, pipefail


_GUARD_PREFIX = re.compile(r"^\s*(if|while|until|elif)\b|^\s*!\s")


def is_protected(code: str) -> bool:
    """管線是否被 if/while/until/! 條件或 ``||`` / ``&&`` 接住。"""
    if _GUARD_PREFIX.search(code):
        return True
    return "||" in code or "&&" in code


# --------------------------------------------------------------------------
# 掃描
# --------------------------------------------------------------------------
def scan_unit(lines: list[tuple[int, str]], errexit: bool, pipefail: bool) -> list[dict]:
    """掃一個 shell 單元。``lines`` 是 (行號, 原始行) 串列。"""
    violations: list[dict] = []
    # 把 `\` 續行併成一個邏輯語句；記住每個邏輯語句的起始行
    logical: list[tuple[int, str]] = []
    buf, start = "", None
    for lineno, raw in lines:
        code = strip_comment(raw).rstrip()
        if start is None:
            start = lineno
        if code.endswith("\\"):
            buf += code[:-1] + " "
            continue
        buf += code
        logical.append((start, buf))
        buf, start = "", None
    if buf.strip():
        logical.append((start or 0, buf))

    prev_stmt: str | None = None
    for lineno, stmt in logical:
        stripped = stmt.strip()
        if not stripped:
            continue
        if stripped.split()[:1] == ["set"]:
            errexit, pipefail = apply_set(stripped, errexit, pipefail)
            prev_stmt = stripped
            continue
        if _PIPESTATUS_RE.search(stripped):
            if (
                errexit
                and pipefail
                and prev_stmt is not None
                and has_bare_pipe(prev_stmt)
                and not is_protected(prev_stmt)
            ):
                violations.append(
                    {
                        "line": lineno,
                        "code": stripped[:120],
                        "pipeline": prev_stmt[:120],
                    }
                )
        prev_stmt = stripped
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


_RUN_RE = re.compile(r"^(\s*)(?:-\s+)?run:\s*\|")
_SHELL_BASH_RE = re.compile(r"^\s*(?:-\s+)?shell:\s*bash\s*$")


def _workflow_run_units(rel: str, text: str) -> list[dict]:
    """把 workflow 的 ``run: |`` 區塊切出來。

    ⚠️ 起始狀態照 GitHub 的預設：``bash -e {0}`` ⇒ errexit 開、pipefail 關；
    同一 step 出現 ``shell: bash`` 才兩者皆開。
    """
    lines = text.splitlines()
    units: list[dict] = []
    for idx, line in enumerate(lines):
        m = _RUN_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        body: list[tuple[int, str]] = []
        j = idx + 1
        while j < len(lines):
            cur = lines[j]
            if cur.strip() and (len(cur) - len(cur.lstrip())) <= indent:
                break
            body.append((j + 1, cur))
            j += 1
        if not body:
            continue
        # 往上找同一 step 的 `shell: bash`
        pipefail = False
        k = idx - 1
        while k >= 0 and lines[k].strip():
            if _SHELL_BASH_RE.match(lines[k]):
                pipefail = True
                break
            if re.match(r"^\s*-\s+name:", lines[k]):
                break
            k -= 1
        units.append(
            {
                "path": f"{rel}:{idx + 1}",
                "kind": "workflow-run",
                "errexit": True,
                "pipefail": pipefail,
                "lines": body,
            }
        )
    return units


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ci", action="store_true", help="有違規時 exit 1")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--repo", default=str(_REPO_ROOT))
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
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
