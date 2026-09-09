#!/usr/bin/env python3
"""list_subprocess_only_modules.py — 哪些模組**只**被 subprocess 測到（coverage 盲點）。

Why this exists
---------------
`pyproject.toml` 的 coverage `source` 是 `["scripts/tools", "components/da-tools/app"]`，
但 **以 subprocess 呼叫工具的測試對 coverage.py 完全不可見**——它預設不追子行程。
結果是一支工具可以有完整且有偵測力的測試，而 coverage 報表把它讀成「從未被 import」
（TRK-379 / [#1746](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1746)）。

⛔ **這正是「量不到」被呈現成「量了沒事」**：coverage delta bot 對這種檔案印的是
「無 per-file 變化」，與「這個檔沒問題」在版面上長得一模一樣。

⚠️ **母體 ≠ 盲點數**。票裡量到「78 個測試檔出現 `sys.executable`」，但那**不是**
78 個盲點：另有一大批檔案**同時**直接 import 同一個模組，重疊多少票裡明寫沒有量過。
本工具產出的就是那份沒有人產生過的清單：**coverage source 範圍內、只被 subprocess
測到、沒有任何 in-process 進入點**的模組。

述詞
----
對 coverage source 內的每個模組 `M`：

- `imported(M)` — 有測試檔以 AST 可見的方式 import 它（`import` / `from … import` /
  `importlib.import_module("…")` / `spec_from_file_location("…")`）。
- `subprocess(M)` — 有測試檔**同時**起子行程（`sys.executable` / `subprocess.`）
  **且**在檔案內文提到該模組的路徑或檔名。

分類：`subprocess(M) and not imported(M)` ⇒ **盲點**。

⚠️ `subprocess(M)` 是字串啟發式（測試檔怎麼組指令沒有統一寫法），所以**它會高估**。
⛔ 因此本工具的清單要用 `--verify` 拿真 coverage 抽驗：對每個候選跑
`pytest <test> --cov=<模組名>`，看是不是真的 `No data was collected`。

⚠️ **量法本身有一個坑，票裡記著、這裡再記一次**：`--cov` 給**路徑**時，**連
in-process 測試都會報 `never imported`**——那是壞掉的儀器不是結果。要給**模組名**。

Usage
-----
::

    python3 scripts/tools/dx/list_subprocess_only_modules.py            # 報告
    python3 scripts/tools/dx/list_subprocess_only_modules.py --json
    python3 scripts/tools/dx/list_subprocess_only_modules.py --verify N # 抽驗 N 支

Exit codes
----------
- ``0`` — 產出了清單（**不**因為有盲點而失敗：本工具是**界定範圍**用的，不是閘門）
- ``2`` — **量不到**：不是 git repo、讀不到 pyproject 的 coverage source、或母體為空。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_CALLER_ERROR  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


def coverage_sources(repo: Path) -> tuple[list[str], set[str]]:
    """從 pyproject.toml 讀 coverage 的 source 與 omit —— 不硬編。"""
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"\[tool\.coverage\.run\](.*?)(?=\n\[|\Z)", text, re.S)
    if not m:
        return [], set()
    block = m.group(1)
    src = re.search(r"source\s*=\s*\[(.*?)\]", block, re.S)
    omit = re.search(r"omit\s*=\s*\[(.*?)\]", block, re.S)
    q = lambda s: re.findall(r'"([^"]+)"', s or "")
    return q(src.group(1) if src else ""), set(q(omit.group(1) if omit else ""))


def tracked(repo: Path, *patterns: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", *patterns],
        capture_output=True, check=True, timeout=120,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def build(repo: Path) -> dict:
    sources, omit = coverage_sources(repo)
    if not sources:
        raise RuntimeError("pyproject.toml 讀不到 [tool.coverage.run] source")

    modules: dict[str, str] = {}
    for src in sources:
        # ⚠️ `**/` 至少要吃一層目錄 ⇒ 只給 `{src}/**/*.py` 會**漏掉該目錄的頂層檔案**。
        # 兩個 pathspec 都給，git ls-files 會取聯集。
        # 釘住：`test_top_level_modules_are_in_the_population` /
        #       `test_top_level_test_files_are_in_the_population`
        for p in tracked(repo, f"{src}/*.py", f"{src}/**/*.py"):
            if p in omit or "/vendor/" in p or "__pycache__" in p:
                continue
            modules[p] = Path(p).stem
    stem_to_paths: dict[str, list[str]] = defaultdict(list)
    for p, stem in modules.items():
        stem_to_paths[stem].append(p)

    tests = [t for t in tracked(repo, "tests/*.py", "tests/**/*.py")
             if Path(t).name.startswith("test_")]

    imported: dict[str, set[str]] = defaultdict(set)
    subproc: dict[str, set[str]] = defaultdict(set)
    spawners: set[str] = set()

    for t in tests:
        text = (repo / t).read_text(encoding="utf-8", errors="replace")
        spawns = "sys.executable" in text or "subprocess." in text
        if "sys.executable" in text:
            spawners.add(t)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for n in names:
                    last = n.split(".")[-1]
                    if last in stem_to_paths:
                        imported[last].add(t)
            for pat in (r'import_module\(\s*["\']([\w.]+)["\']',
                        r'spec_from_file_location\(\s*["\']([\w.]+)["\']'):
                for m in re.finditer(pat, text):
                    last = m.group(1).split(".")[-1]
                    if last in stem_to_paths:
                        imported[last].add(t)
        if spawns:
            for stem, paths in stem_to_paths.items():
                if any(p in text for p in paths) or f"{stem}.py" in text:
                    subproc[stem].add(t)

    blind = sorted(s for s in subproc if s not in imported)
    return {
        "sources": sources,
        "modules": len(modules),
        "stems": len(stem_to_paths),
        "tests": len(tests),
        "tests_with_sys_executable": len(spawners),
        "blind_spots": [
            {"module": stem_to_paths[s][0], "stem": s, "tests": sorted(subproc[s])}
            for s in blind
        ],
        "both": sorted(s for s in subproc if s in imported),
        "import_only": sorted(s for s in imported if s not in subproc),
        "untested": sorted(s for s in stem_to_paths if s not in subproc and s not in imported),
    }


def verify(repo: Path, data: dict, limit: int) -> list[dict]:
    """對前 N 個候選跑真 coverage —— ⚠️ `--cov` 必須給**模組名**不是路徑。"""
    out = []
    for entry in data["blind_spots"][:limit]:
        test = entry["tests"][0]
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", test, f"--cov={entry['stem']}",
             "--cov-report=term", "--cov-fail-under=0", "-q", "-p", "no:randomly"],
            cwd=repo, capture_output=True, text=True, timeout=900,
        )
        blob = proc.stdout + proc.stderr
        out.append({
            "module": entry["module"],
            "test": test,
            "no_data": "No data was collected" in blob,
        })
    return out


def main(argv: list[str] | None = None) -> int:
    # ⛔ try_utf8_stdout() 要在 argparse 之前：`--help` 裡的 CJK 在 legacy Windows
    # console（cp950/cp936）會在 argparse 印出來之前就 UnicodeEncodeError。
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=i18n_text(
            "列出 coverage source 範圍內、只被 subprocess 測到的模組——"
            "那些模組對 coverage.py 完全不可見，報表把它們讀成「從未被 import」。",
            "List modules inside the coverage source that are only exercised "
            "through subprocess. coverage.py cannot see them, so the report "
            "reads them as never imported.",
        ),
    )
    ap.add_argument("--repo", default=str(_REPO_ROOT),
                    help=i18n_text("要掃描的 repo 根目錄", "repository root to scan"))
    ap.add_argument("--json", action="store_true",
                    help=i18n_text("輸出 JSON", "emit JSON"))
    ap.add_argument("--verify", type=int, default=0, metavar="N",
                    help=i18n_text("對前 N 個候選跑真 coverage 抽驗",
                                   "spot-check the first N candidates with real coverage"))
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"[subproc-only] ⛔ 量不到：{repo} 不是 git repo", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        data = build(repo)
    except (RuntimeError, subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        print(f"[subproc-only] ⛔ 量不到：{exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not data["stems"] or not data["tests"]:
        print("[subproc-only] ⛔ 量不到：母體是空的（模組或測試檔為 0）。"
              "工具失能與零命中長得一樣。", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if args.verify:
        data["verified"] = verify(repo, data, args.verify)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"[subproc-only] coverage source: {', '.join(data['sources'])}")
    print(f"  模組（去重後的 stem）        : {data['stems']}")
    print(f"  測試檔                       : {data['tests']}"
          f"（含 sys.executable: {data['tests_with_sys_executable']}）")
    print()
    print(f"  ⛔ 只被 subprocess 測到（盲點）: {len(data['blind_spots'])}")
    print(f"     兩種都有（重疊）           : {len(data['both'])}")
    print(f"     只被 import 測到           : {len(data['import_only'])}")
    print(f"     兩種都沒有                 : {len(data['untested'])}")
    print()
    print("  ⚠️ 母體 ≠ 盲點數：含 sys.executable 的測試檔數**不等於**盲點數，")
    print("     因為多數模組同時有 in-process 進入點。上面第二列就是那個重疊。")
    print()
    for e in data["blind_spots"]:
        print(f"    {e['module']}")
    if "verified" in data:
        print()
        print("  -- 真 coverage 抽驗（--cov 給模組名，給路徑會是壞掉的儀器）--")
        for v in data["verified"]:
            mark = "No data was collected（確認是盲點）" if v["no_data"] else "有資料（本工具高估）"
            print(f"    {v['module']}: {mark}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
