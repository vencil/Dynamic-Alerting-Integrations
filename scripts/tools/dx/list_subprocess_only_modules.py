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

⚠️ `subprocess(M)` 是字串啟發式（測試檔怎麼組指令沒有統一寫法），**兩個方向都會錯**。
⛔ 這句原本只寫「它會高估」——那是**方向上的過度宣稱**，盲審用一個真實形狀打穿了。

**高估（假陽性）**：只要測試檔的**任何文字**出現 `<stem>.py`，該模組就被算成被 subprocess
測到——一句提到 `legacy_report.py` 的 docstring 就足以把 `report` 列成盲點，還附一份指向
那個無關測試檔的 `tests` 清單。釘住：`test_prose_mentioning_a_stem_creates_a_false_positive`。

**低估（假陰性，⛔ 更危險）**：測試檔若**間接**組出路徑——例如 `conftest.py` 放
``TOOL = Path("scripts")/"tools"/"ops"/"mytool.py"``，測試檔只 import 那個常數——檔案內文
就沒有 `mytool.py` 這串字，該模組於是落進 **``untested``（無害桶）而不是 ``blind_spots``**。
一個真的盲點被歸類成「根本沒測試」，方向與本工具的用途相反。
釘住：`test_indirectly_built_paths_are_a_false_negative`。

⛔ **這份清單是待查名單，不是判定**，而且它**兩邊都漏**：名單上的不一定是盲點，
不在名單上的也不一定不是。

⛔ **本工具曾有一個 `--verify` 子功能（拿真 coverage 抽驗），已移除**。三個各自都足夠的
理由，全部量過：

1. **結構上只能修假陽性**。它只走訪 `blind_spots`，所以任何被**錯誤排除**在清單外的模組
   （見下面「已知界線」）對它永遠不可見。
2. **它自己就是壞掉的儀器**。它跑 `--cov=<stem>`，而 stem 撞到已安裝套件時量到的是那個
   套件：對一支叫 `json.py` 的專案工具實測，`--cov=json` 量到的是
   `/usr/lib/python3.11/json/*`（22%／3%／0%／0%）⇒ 它會回報「有資料 ⇒ 本工具高估」，
   而其實它根本沒量到受測模組。這正是本 docstring 原本只針對「`--cov` 給路徑」提出的
   警告，**同一個機制在它推薦的模組名形式上照樣成立**。
3. **零測試釘住**。mutation 實測：把 `--cov={stem}` 改回它自己警告過的 `--cov={module}`，
   全套 20 格**仍然全過**。

⇒ 想抽驗請自己跑，並且**確認 `--cov` 指到的真的是你要的那個模組**（`--cov-report=term`
會把量到的檔案路徑印出來，看那個路徑）。

⛔ **已知界線（會造成假陰性，也就是真盲點被吃掉）**：分類以檔名 stem 為鍵。⑴ 兩個不同的
專案檔共用 stem 會被合併成一筆（實測：`a/dup.py` 只被 subprocess 測、`b/dup.py` 被 import，
結果整個 stem 記成 `both`，真盲點連痕跡都不留）；⑵ 測試檔 `import` 一個**同名的 stdlib
或第三方套件**也會被算成 in-process 進入點（實測：`import json` 讓專案的
`scripts/tools/ops/json.py` 從盲點變成 `both`）。這兩條**本輪未修**。

Usage
-----
::

    python3 scripts/tools/dx/list_subprocess_only_modules.py            # 報告
    python3 scripts/tools/dx/list_subprocess_only_modules.py --json

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
import tomllib
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
    """從 pyproject.toml 讀 coverage 的 source 與 omit —— 用 stdlib ``tomllib``。

    ⛔ 這裡**不能**用 regex。先前的版本用 ``re.search`` 配一個 ``[tool.coverage.run]`` 的 header pattern
    加 ``re.findall(r'"([^"]+)"')``，與真 TOML 有四種已量到的分歧，其中兩種是
    **靜默拿錯母體**（比「壞掉」更糟，因為它會算出一個看起來正常的答案）：

    ==========================================  ==================  ==================
    輸入（都是合法 TOML）                        tomllib             舊的 regex
    ==========================================  ==================  ==================
    ``source = ['a', 'b']``（單引號）            ``['a','b']``       ``[]`` → rc 2
    ``[tool.coverage]`` + ``run.source = […]``   讀到                ``[]`` → rc 2
    陣列裡有 ``# not "b"`` 這樣的註解             ``['a']``           ``['a','b']`` ⚠️
    別處字串裡含 ``[tool.coverage.run]``         真的那個            那個假的 ⚠️
    ==========================================  ==================  ==================

    釘住這四案的是 ``test_toml_parsing_matches_tomllib``（參數化，tomllib 當 oracle）。

    ⚠️ TOML 規格要求檔案是 UTF-8。非 UTF-8 會讓 ``tomllib.load`` 丟
    ``UnicodeDecodeError``——它是 ``ValueError`` 的子類、**不是** ``OSError``，
    所以呼叫端的 ``except`` 攔不到，會以裸 traceback + rc 1 逃出去。rc 1 在本 repo
    是 ``EXIT_VIOLATION``（量了、有問題），而這其實是「量不到」⇒ 這裡轉成
    ``RuntimeError``，讓它走 rc 2。釘住：``test_non_utf8_pyproject_is_rc2``。
    """
    path = repo / "pyproject.toml"
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"pyproject.toml 不是合法的 TOML：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            f"pyproject.toml 不是合法的 UTF-8（TOML 規格要求 UTF-8）：{exc}"
        ) from exc

    # ⛔ 逐層檢查，不能只檢查葉子。`doc.get("tool", {}).get("coverage", {})` 這種鏈式
    #   寫法在 `tool` 或 `tool.coverage` 是純量時會丟 `AttributeError`——它是
    #   `ValueError`／`OSError` 之外的第三種，呼叫端的 `except` 攔不到 ⇒ 裸 traceback
    #   + rc 1，正是本函式宣稱已經收口的那個病。⚠️ 第一版只守了葉子的 source/omit，
    #   盲審用 `tool = "not-a-table"` 一句就打穿。釘住：`test_non_table_*_is_rc2`。
    node: object = doc
    for key in ("tool", "coverage", "run"):
        if not isinstance(node, dict):
            raise RuntimeError(
                f"pyproject.toml 的 [tool.coverage.run] 路徑上 {key!r} 的上層不是 table"
            )
        node = node.get(key, {})
    if not isinstance(node, dict):
        raise RuntimeError("pyproject.toml 的 [tool.coverage.run] 不是 table")
    run = node

    def _strs(value: object, key: str) -> list[str]:
        # coverage.py 的 source/omit 是字串陣列。給了別的形狀就是設定寫錯，
        # 而「設定寫錯」屬於量不到，不該被當成零命中。
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise RuntimeError(
                f"[tool.coverage.run] {key} 必須是字串陣列，讀到 {type(value).__name__}"
            )
        return list(value)

    return _strs(run.get("source"), "source"), set(_strs(run.get("omit"), "omit"))


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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
