#!/usr/bin/env python3
"""list_subprocess_only_modules.py — 哪些模組**只**被 subprocess 測到（coverage 盲點）。

Why this exists
---------------
coverage `source` 涵蓋 `scripts/tools` 與 `components/da-tools/app`，但 **以 subprocess
呼叫工具的測試對 coverage.py 完全不可見**——它預設不追子行程。結果是一支工具可以有完整
且有偵測力的測試，而 coverage 報表把它讀成「從未被 import」
（TRK-379 / [#1746](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1746)）。

⛔ **這正是「量不到」被呈現成「量了沒事」**：coverage delta bot 對這種檔案印的是
「無 per-file 變化」，與「這個檔沒問題」在版面上長得一模一樣。

⚠️ **母體 ≠ 盲點數**：出現 `sys.executable` 的測試檔遠多於真盲點，因為多數模組**同時**
有 in-process 進入點。本工具產出的就是扣掉重疊之後的那份清單。⛔ 絕對數字本檔一律不寫
（母體就是這棵樹，寫死的計數必然漂）——跑一次就是當下真值。

述詞
----
對 coverage source 內的每個模組 `M`：

- `imported(M)` — 有測試檔以 AST 可見的方式 import 它（`import` / `from … import` /
  `importlib.import_module("…")` / `spec_from_file_location("…")`）。
- `subprocess(M)` — 有測試檔**同時**起子行程（`sys.executable` / `subprocess.`）
  **且**在檔案內文提到該模組的路徑或檔名。

分類：`subprocess(M) and not imported(M)` ⇒ **盲點**。

⚠️ `subprocess(M)` 是字串啟發式（測試檔怎麼組指令沒有統一寫法），**兩個方向都會錯**：

- **高估（假陽性）**：測試檔的**任何文字**出現 `<stem>.py` 就算數——一句提到
  `legacy_report.py` 的 docstring 足以把 `report` 列成盲點。
  釘住：`test_prose_mentioning_a_stem_creates_a_false_positive`。
- **低估（假陰性，⛔ 更危險）**：路徑若是**間接**組出來的（`conftest.py` 放
  ``Path("scripts")/"tools"/"ops"/"mytool.py"``），檔案內文就沒有那串字，該模組落進
  ``untested``（無害桶）而不是 ``blind_spots``——真盲點被歸類成「根本沒測試」。
  釘住：`test_indirectly_built_paths_are_a_false_negative`。

⛔ **這份清單是待查名單，不是判定**，而且它**兩邊都漏**。

⛔ **不要重新引入 `--verify`**（拿真 coverage 抽驗那個子功能，已移除）。三個各自都足夠的
理由：⑴ 它只走訪 `blind_spots`，被**錯誤排除**在清單外的模組對它結構上不可見；⑵ 它跑
`--cov=<stem>`，stem 撞到已安裝套件時量到的是**那個套件**——對專案的 `json.py` 實測
`--cov=json` 量到 `/usr/lib/python3.11/json/*`，而它報什麼取決於那支測試檔碰巧有沒有用到
stdlib `json`，**兩個方向都會壞**（沒資料 ⇒ 假確認；有資料 ⇒ 假否定）；⑶ 它零測試釘住。

⇒ 想抽驗請自己跑，並且**確認 `--cov` 指到的真的是你要的那個模組**（`--cov-report=term`
會把量到的檔案路徑印出來，看那個路徑）。

已知界線
--------
⛔ **以下六條都造成假陰性（真盲點被吃掉），且本工具不修**。分類以檔名 stem 為鍵：

⑴ 兩個專案檔共用 stem 會被併成一筆——只被 subprocess 測的那個連痕跡都不留。
   釘住：`test_known_limit_two_project_files_sharing_a_stem_are_merged`。
⑵ 測試檔 `import` 一個**同名的 stdlib 或第三方套件**也算成 in-process 進入點。
   釘住：`test_known_limit_a_stdlib_import_shadows_a_project_stem` 與
   `test_known_limit_a_third_party_import_also_shadows_a_project_stem`。
⑶ `from pkg import name` 的 `name` 從 AST 看不出是**模組**還是**符號**，同名符號同樣遮蔽。
   釘住：`test_known_limit_a_from_imported_symbol_shadows_a_module_stem`。
   ⚠️ 這條是**換來的**：不看 `node.names` 會讓 `from scripts.tools.ops import mytool` 這種
   真進入點被漏掉。兩個方向都會錯，這裡選了這一側。
⑷ `ast.walk` **不看可達性**：`if TYPE_CHECKING:` 之下與函式 body 內的 import 執行期可能
   永遠不跑，卻都算成進入點。兩半各有一格：
   `test_known_limit_ast_walk_ignores_reachability` 與
   `test_known_limit_a_function_body_import_also_counts_as_an_entry_point`。
   ⚠️ 只修 `TYPE_CHECKING` 會給出**部分覆蓋與虛假的安全感**——函式內 import 同構且無法從
   AST 判定 ⇒ 整條列為界線。
⑸ `from __future__ import annotations` 讓任何名為 `annotations.py` 的模組**永久**被遮蔽。
   機制同 ⑵／⑶，但普遍得多——它是多數測試檔的第一行。
   釘住：`test_known_limit_future_annotations_shadows_a_module_named_annotations`
   （該格對**當下**母體重算並斷言它仍是多數，紅的時候印出實際比例）。
⑹ 測試檔裡的**相對** import（`node.level > 0`）在**本 repo 的設定下**指不到 source root
   的模組（source root 與 `tests/` 不相交）⇒ 算成進入點是撞名。
   ⛔ **這是設定的性質，不是結構定理**：`source = ["tests"]` 時它就指得到，而 `build()` 的
   測試 pathspec 是**寫死**的、與 `source` 互不參照，沒有東西保證兩者不相交。兩側各一格：
   `test_known_limit_a_relative_import_in_tests_can_never_name_a_source_module`（本 repo 設定，
   自己會斷言不相交）與 `test_a_relative_import_does_reach_a_module_when_source_is_tests`（反例）。

⛔ **不要為 ⑵⑶⑷⑸⑹ 再寫一版述詞。** 它們全是同一個 decidability 問題的實例：從 AST 看不出
`import X` 解析到誰。要真的修得換到有權威 oracle 的那一面（import 系統／coverage 自己的
量測），那是另一張票。

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
import fnmatch
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

    ⛔ 這裡**不能**用 regex。與真 TOML 有四種已量到的分歧，其中兩種是**靜默拿錯母體**
    （比「壞掉」更糟，因為它會算出一個看起來正常的答案）：

    ==========================================  ==================  ==================
    輸入（都是合法 TOML）                        tomllib             regex
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
    #   + rc 1，正是本函式要收口的那個病。
    #   釘住：`test_non_table_on_the_tool_coverage_run_path_is_rc2`。
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


def _omitted(path: str, omit: set[str]) -> bool:
    """coverage.py 的 ``omit`` 是 **shell-style filename pattern**，不是字面相等。

    ⛔ **不能寫成 ``p in omit``**（字面相等）：對任何帶 wildcard 的 omit 都無感，於是一個
    coverage.py **根本不會量**的檔被回報成「coverage 盲點」。那是**類別錯誤**——它不是
    「只被 subprocess 測到所以看不見」，它是**被刻意排除在量測之外**。
    ⛔ 也**不要**補 ``"/vendor/" in p`` 這類 hardcode 子字串來蓋住它：那讓覆蓋來自寫死的
    字串而不是 ``omit`` 本身，換一條 glob omit 就破。

    ⛔ **這裡用 stdlib ``fnmatch``，而它不等於 coverage 自己的 ``GlobMatcher``**
    （**兩個方向都分歧**，下表由 `test_known_limit_fnmatch_diverges_from_coverage_in_both_directions` 對**當下裝的** coverage 逐列重算）：

    ===============================  =======================  =========  ==========
    path                             pattern                  fnmatch    coverage
    ===============================  =======================  =========  ==========
    ``a/c.py``                       ``a/**/c.py``            False      **True**
    ``vendor/x.py``                  ``*/vendor/*``           False      **True**
    ``__pycache__/x.py``             ``*/__pycache__/*``      False      **True**
    ``a/b/d/c.py``                   ``a/*/c.py``             **True**   False
    ``scripts/tools/gen/d/x.py``     ``scripts/tools/*/x.py`` **True**   False
    ``scripts/tools/vendor/x.py``    ``*/vendor/*``           True       True
    ===============================  =======================  =========  ==========

    ⛔ **兩個方向都會壞，沒有「安全側」可以倚賴**：``fnmatch`` 少配（上半）會把一個被
    omit 的檔回報成盲點（吵，但看得見）；多配（下半，因為它的 ``*`` **跨目錄分隔符**）
    會把一個真盲點**靜默**吃掉。

    ⚠️ **不要改成呼叫 ``coverage.files.GlobMatcher``。** 那是伸手進第三方 internals，
    而且它**沒有買到任何東西**——對本 repo 真實的母體與 ``omit``，兩個 matcher 排除的
    集合完全相同（對稱差為空集合）。代價則是兩個真實的破口：``GlobMatcher(...)`` 會對
    coverage 自己 glob 文法不收的 omit（如 ``"***"``）丟 ``ConfigError``，其 MRO 不含
    ``RuntimeError`` / ``OSError`` ⇒ 逃出 ``main()`` 的 catch-list、**裸 traceback + rc 1**，
    而本檔的契約說那種情況要 rc 2；而它取不到時的退路是**靜默**的，兩個 matcher 對同一個
    路徑給相反答案卻零訊號——直接違反「量不到與量了沒事必須可區分」。

    ⇒ 這支工具**不自稱**是 coverage matcher 的等價物。「本 repo 的 omit 設定有沒有踩進
    分歧區」改由測試用 coverage 自己當 oracle 去問，而且是在**測試裡**問、不在熱路徑上問：
    設定哪天漂進分歧區，那格會紅並指名哪一條 pattern、哪一個檔、哪個方向。

    釘住：``test_wildcard_omit_is_honoured``（漏判側）、
    ``test_non_matching_omit_does_not_exclude``（誤排除側）、
    ``test_the_real_omit_config_stays_inside_the_matchers_agreement_region``（分歧區守衛）、
    ``test_known_limit_fnmatch_diverges_from_coverage_in_both_directions``（兩個方向的現況）。
    """
    if not omit:
        return False
    return any(fnmatch.fnmatch(path, pat) for pat in omit)


def build(repo: Path) -> dict:
    sources, omit = coverage_sources(repo)
    if not sources:
        raise RuntimeError("pyproject.toml 讀不到 [tool.coverage.run] source")

    modules: dict[str, str] = {}
    for src in sources:
        # ⚠️ `**/` 至少要吃一層目錄 ⇒ **只給 `{src}/**/*.py` 會漏掉該目錄的頂層檔案**。
        # 兩個 pathspec 都給（git ls-files 取聯集）。⛔ 不要「精簡」成一個：哪一個是
        # 全集取決於 git 的 pathspec 設定（預設 `*` 跨 `/`，`:(glob)` magic 則否），
        # 而漏掉頂層那一次是**靜默**的——母體少一截，報告仍然長得正常。
        # 釘住：`test_top_level_modules_are_in_the_population` /
        #       `test_top_level_test_files_are_in_the_population`
        for p in tracked(repo, f"{src}/*.py", f"{src}/**/*.py"):
            if _omitted(p, omit):
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
                elif isinstance(node, ast.ImportFrom):
                    # ⚠️ 這裡**不分 `node.level`**，而那是已知的假陰性通道（界線 ⑹），
                    #   不是疏忽——理由與「不要再寫一版述詞」的理由都在模組 docstring。
                    #   釘住：`test_known_limit_a_relative_import_in_tests_can_never_name_a_source_module`
                    #   與反例 `test_a_relative_import_does_reach_a_module_when_source_is_tests`。
                    # ⛔ 不能只看 `node.module`。`from scripts.tools.ops import mytool`
                    #   的 `node.module` 末段是 `ops`，真正的模組名在 `node.names` 裡；
                    #   只看前者會讓一個**確實有 in-process 進入點**的模組被列進
                    #   `blind_spots`（假陽性）。
                    #   釘住：`test_package_level_from_import_counts_as_in_process`。
                    names = ([node.module] if node.module else []) \
                        + [a.name for a in node.names]
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
