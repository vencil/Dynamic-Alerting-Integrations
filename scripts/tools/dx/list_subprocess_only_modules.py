#!/usr/bin/env python3
"""list_subprocess_only_modules.py — 每個模組被哪一種進入點執行到（以 coverage 實測為準）。

TRK-379 / [#1746](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1746)。

本工具回答的問題
----------------
對 coverage source 內的每個模組**檔案**，整輪測試把它執行起來的進入點是哪一種：
in-process（測試直接 import 後呼叫）、subprocess（測試起子行程跑它）、兩者、或都沒有。

⛔ **這不再是「盲點」清單。** 自 `tests/conftest.py` 接上 subprocess coverage 之後，
以子行程執行的模組**對 coverage 是可見的**——「只走 subprocess」仍然為真，但它是一種
**進入點型態**，不是一個量測缺口。把它讀成盲點會高估問題。真正的缺口只剩 `unexecuted`
（見下方「本工具答不出來的事」）。

判定方式：權威 oracle，不是述詞
--------------------------------
`tests/conftest.py` 把子行程的 coverage 設定標上 `context = "subprocess"`（機制見該檔
註解），於是**每一行被記錄的程式碼都自己帶著它是由哪種行程執行的**。本工具只是去讀
那份資料：

- `{"subprocess"}`      ⇒ `subprocess_only`
- `{""}`                ⇒ `in_process_only`
- `{"", "subprocess"}`  ⇒ `both`
- 完全沒有執行到的行    ⇒ `unexecuted`

⛔ **前一版以 AST stem-matching 判定，連同它六條已知界線，已於本版整組退役。**
那六條全是同一個 decidability 問題的實例（從 AST 判不出 `import X` 解析到誰），而本版
以**檔案路徑**為鍵、以實際執行為據，該問題在這裡不存在。不要把它們搬回來。

⛔ **不要重新引入 `--verify` 那個形狀**（已移除的舊子功能：對每個 stem 跑 `--cov=<stem>`
抽驗）。它錯在兩處而本版兩處都不同：它只走訪 `blind_spots` 所以結構上看不到被錯誤排除
的模組（本版走訪**整個母體**），且 `--cov=<stem>` 在 stem 撞到已安裝套件時量到的是那個
套件（本版不用 stem，讀的是整輪實跑的資料檔）。

本工具答不出來的事（刻意留白，不是疏忽）
----------------------------------------
⚠️ `unexecuted` **合併了兩件事**：真的沒有任何測試碰它、以及有測試碰它但那次執行
**沒被記錄**。後者的已知成因是測試以 `env={...}` 從頭組環境而不帶 `os.environ`，子行程
因此收不到 `COVERAGE_PROCESS_CONFIG`。⛔ 從 coverage 資料**無法**區分這兩者——那正是本
工具倚賴的 oracle 的邊界。要縮小它只能去改那些測試，不能靠這裡多寫一條述詞。

⚠️ 本工具**不再回報「哪些測試檔碰到這個模組」**。coverage 的靜態 context 記錄的是行程
種類不是測試身分；要那個資訊得改用 `--cov-context=test` 的動態 context，那會取代本工具
倚賴的靜態標記。⇒ 兩者擇一，本工具選了前者。

⚠️ 讀到的是**某一次**測試執行留下的資料。它是否對應當下的樹，本工具不驗證也無從驗證；
母體與資料完全不相交時走 rc 2，但「部分過期」讀起來會正常。⇒ 要現況就先重跑測試。

Usage / Exit codes
------------------
::

    # 先產生資料（conftest 會在 --cov 時自動接上 subprocess coverage）
    pytest tests/ --cov
    python3 scripts/tools/dx/list_subprocess_only_modules.py [--json] [--coverage-data PATH]

- ``0`` — 產出了分類（**不**因為有 `subprocess_only` 或 `unexecuted` 而失敗：這是界定
  範圍用的報告，不是閘門）
- ``2`` — **量不到**：不是 git repo、讀不到 pyproject 的 coverage source、母體為空、
  coverage 拒絕 ``omit`` 裡的某條 pattern、**找不到或讀不懂 coverage 資料檔**、
  **資料裡沒有任何 `subprocess` context**（⇒ 它是在未接線的情況下產生的，回答不了本
  問題）、**資料出現未知的 context**（⇒ 分類規則不再是全稱的）、或**母體與資料完全
  不相交**（⇒ 這份資料描述的不是這棵樹）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
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

    ⛔ **不能用 regex。** 與真 TOML 有四種已量到的分歧，其中兩種是**靜默拿錯母體**
    （比壞掉更糟，它會算出一個看起來正常的答案）。四案由
    ``test_toml_parsing_matches_tomllib`` 以 ``tomllib`` 當 oracle 逐案釘住：單引號陣列、
    ``[tool.coverage]`` + ``run.source``、陣列裡的註解、別處字串裡的假 header。

    ⚠️ 非 UTF-8 會讓 ``tomllib.load`` 丟 ``UnicodeDecodeError``——它是 ``ValueError`` 的
    子類、**不是** ``OSError``，呼叫端攔不到 ⇒ 裸 traceback + rc 1，而這其實是「量不到」。
    這裡轉成 ``RuntimeError`` 走 rc 2。釘住：``test_non_utf8_pyproject_is_rc2``。
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


def omit_matcher(omit: set[str]) -> "Callable[[str], bool] | None":
    """coverage 自己的 matcher；`omit` 為空時回 ``None``（完全不碰 coverage）。

    ⛔ **權威 oracle 是 coverage 的 ``GlobMatcher``，不是 ``fnmatch``。** 兩者實測**兩個
    方向都分歧**——``fnmatch`` 的 ``*`` **跨目錄分隔符**，多配時會把一個真盲點靜默吃掉
    （``a/b/d/c.py`` 對 ``a/*/c.py``：fnmatch 配到、coverage 不配）；而它不認 coverage 的
    ``**``，少配時把被 omit 的檔回報成盲點。⇒ 用 ``fnmatch`` 近似**沒有安全側**，而這支
    工具的整個用途就是回答「coverage 看不看得到這個檔」。

    ⛔ **coverage 拒絕一條 omit pattern 時，那是「量不到」，不是換個 matcher 的理由。**
    ``GlobMatcher`` 對自己 glob 文法不收的 pattern（``"***"``、未閉合的 ``[`` 等）在
    **建構期**丟 ``ConfigError``——它的 MRO 不含 ``RuntimeError``／``OSError``，直接讓它
    逃出去會是裸 traceback + rc 1（本 repo 的 ``EXIT_VIOLATION``＝量了有問題），而真相是
    **設定讀不懂所以量不到**。⇒ 這裡轉成 ``RuntimeError`` 走 rc 2，與 TOML 解析失敗、
    omit 形狀不對走的是同一條路。

    ⚠️ ``omit`` 非空卻匯入不到 ``coverage`` 時同樣是 rc 2：沒有它就沒有 omit 的語意，
    而**靜默改用別的 matcher 會讓「量不到」與「量了沒事」長得一樣**。
    ⚠️ matcher 只建一次（在 `build()` 裡），不是每個檔重建一次。

    釘住：``test_wildcard_omit_is_honoured``（漏判側）、
    ``test_non_matching_omit_does_not_exclude``（誤排除側）、
    ``test_a_coverage_rejected_omit_pattern_is_rc2``（拒絕 ⇒ rc 2 而非 rc 1）、
    ``test_the_matcher_follows_coverage_not_fnmatch``（兩者分歧時跟著 coverage）。
    """
    if not omit:
        return None
    try:
        from coverage.files import GlobMatcher  # type: ignore[import-not-found]
    except Exception as exc:
        raise RuntimeError(
            f"[tool.coverage.run] omit 有 {len(omit)} 條 pattern，但匯入 coverage 失敗，"
            f"因此無從得知 omit 的語意：{exc}"
        ) from exc

    patterns = sorted(omit)
    try:
        matcher = GlobMatcher(patterns, "omit")
    except Exception as exc:
        raise RuntimeError(
            f"coverage 不接受 [tool.coverage.run] omit 裡的 pattern（{patterns}）：{exc}"
        ) from exc

    def _match(path: str) -> bool:
        try:
            return bool(matcher.match(path))
        except Exception as exc:
            raise RuntimeError(
                f"coverage 的 omit matcher 對 {path!r} 失敗：{exc}"
            ) from exc

    return _match


# 子行程的 coverage 設定在 tests/conftest.py 標上這個 context。⛔ 兩邊是同一個字串
# 的兩份寫法；漂掉時本工具會在「未知 context」那條走 rc 2 而不是靜默分錯桶——失敗是
# 大聲的，但它仍然是兩份，改一邊要改另一邊。
SUBPROCESS_CONTEXT = "subprocess"
IN_PROCESS_CONTEXT = ""

BUCKETS = ("subprocess_only", "both", "in_process_only", "unexecuted")


def read_coverage_data(path: Path):
    """讀 coverage 資料檔；讀不到一律轉成 ``RuntimeError``（呼叫端走 rc 2）。

    ⛔ 這裡的每一種失敗都是「量不到」，不是「量了沒事」。檔案不存在最常見的原因是
    **還沒跑過測試**，而那和「跑了但沒有 subprocess」在分類結果上長得一模一樣（兩者都
    會讓每個模組落進 ``unexecuted``）⇒ 必須在讀取階段就分開。

    釘住：``test_missing_coverage_data_is_rc2`` / ``test_unreadable_coverage_data_is_rc2``。
    """
    try:
        import coverage  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - 環境缺 coverage
        raise RuntimeError(f"匯入 coverage 失敗，無從讀取量測資料：{exc}") from exc

    if not path.exists():
        raise RuntimeError(
            f"找不到 coverage 資料檔：{path}。先跑 `pytest tests/ --cov` 產生它"
            "（conftest 會在 --cov 時自動接上 subprocess coverage）"
        )
    data = coverage.CoverageData(basename=str(path))
    try:
        data.read()
    except Exception as exc:
        raise RuntimeError(f"coverage 資料檔讀不懂（{path}）：{exc}") from exc
    return data


def _relative_to(repo: Path, measured: str) -> "str | None":
    """把 coverage 記錄的絕對路徑折回 repo 相對路徑；不在 repo 內回 ``None``。

    ⚠️ 用 ``os.path.relpath`` 之後檢查 ``..``，不是用字串 prefix 比對——後者會把
    ``/repo-backup/x.py`` 當成 ``/repo`` 底下的檔。
    """
    try:
        rel = os.path.relpath(os.path.realpath(measured), os.path.realpath(repo))
    except ValueError:  # 跨磁碟機（Windows）
        return None
    if rel.startswith(".."):
        return None
    return rel.replace(os.sep, "/")


def contexts_by_module(repo: Path, data) -> dict:
    """repo 相對路徑 -> 該檔被記錄到的 context 集合（只含真的執行過的行）。"""
    out = {}
    for measured in data.measured_files():
        rel = _relative_to(repo, measured)
        if rel is None:
            continue
        ctxs = set()
        for line_ctxs in data.contexts_by_lineno(measured).values():
            ctxs.update(line_ctxs)
        out[rel] = ctxs
    return out


def population(repo: Path) -> list:
    """coverage source 內、未被 omit 排除、且被 git 追蹤的所有模組檔案路徑。"""
    sources, omit = coverage_sources(repo)
    if not sources:
        raise RuntimeError("pyproject.toml 讀不到 [tool.coverage.run] source")
    is_omitted = omit_matcher(omit)
    modules = set()
    for src in sources:
        # ⚠️ `**/` 至少要吃一層目錄 ⇒ **只給 `{src}/**/*.py` 會漏掉該目錄的頂層檔案**。
        # 兩個 pathspec 都給（git ls-files 取聯集）。⛔ 不要「精簡」成一個：哪一個是
        # 全集取決於 git 的 pathspec 設定，而漏掉頂層那一次是**靜默**的——母體少一截，
        # 報告仍然長得正常。釘住：`test_top_level_modules_are_in_the_population`。
        for path in tracked(repo, f"{src}/*.py", f"{src}/**/*.py"):
            if is_omitted is not None and is_omitted(path):
                continue
            modules.add(path)
    return sorted(modules), sources


def build(repo: Path, data_path: Path) -> dict:
    modules, sources = population(repo)
    if not modules:
        raise RuntimeError("母體是空的（coverage source 內沒有任何被追蹤的 .py）")

    data = read_coverage_data(data_path)

    # ⛔ 未知 context ⇒ rc 2，不是丟進 else 桶。分類規則只在 context 集合是
    #   {"", "subprocess"} 的子集時才是全稱的；多出任何一個（例如有人開了
    #   --cov-context=test 的動態 context）就代表這份資料的語意不是本工具假設的那個。
    #   釘住：`test_an_unknown_context_is_rc2`。
    measured_contexts = set(data.measured_contexts())
    unknown = measured_contexts - {IN_PROCESS_CONTEXT, SUBPROCESS_CONTEXT}
    if unknown:
        raise RuntimeError(
            f"coverage 資料裡有本工具不認得的 context {sorted(unknown)}；"
            f"分類規則只涵蓋 {IN_PROCESS_CONTEXT!r} 與 {SUBPROCESS_CONTEXT!r}"
        )
    if SUBPROCESS_CONTEXT not in measured_contexts:
        raise RuntimeError(
            f"coverage 資料裡沒有任何 {SUBPROCESS_CONTEXT!r} context ⇒ 它是在未接上 "
            "subprocess coverage 的情況下產生的，回答不了「哪一種進入點」這個問題。"
            "確認 tests/conftest.py 的接線仍在，然後重跑 `pytest tests/ --cov`"
        )

    ctx_map = contexts_by_module(repo, data)

    # ⛔ 母體與資料完全不相交 ⇒ 這份資料描述的不是這棵樹（例如從別的 worktree 複製
    #   過來的 .coverage）。全部落進 unexecuted 會長得像「整個 repo 都沒測試」，那是
    #   一個看起來正常的錯答案。釘住：`test_data_describing_another_tree_is_rc2`。
    if not any(m in ctx_map for m in modules):
        raise RuntimeError(
            f"coverage 資料檔（{data_path}）與母體完全不相交："
            f"{len(modules)} 個模組沒有任何一個出現在資料裡"
        )

    buckets = {name: [] for name in BUCKETS}
    for module in modules:
        ctxs = ctx_map.get(module) or set()
        if not ctxs:
            buckets["unexecuted"].append(module)
        elif ctxs == {SUBPROCESS_CONTEXT}:
            buckets["subprocess_only"].append(module)
        elif ctxs == {IN_PROCESS_CONTEXT}:
            buckets["in_process_only"].append(module)
        elif ctxs == {IN_PROCESS_CONTEXT, SUBPROCESS_CONTEXT}:
            buckets["both"].append(module)
        else:  # pragma: no cover - 上面的 unknown 守衛已經攔掉
            raise RuntimeError(
                f"{module} 的 context 集合 {sorted(ctxs)} 不在分類規則涵蓋範圍內"
            )

    return {
        "sources": sources,
        "coverage_data": str(data_path),
        "modules": len(modules),
        "measured_contexts": sorted(measured_contexts),
        **{name: buckets[name] for name in BUCKETS},
    }


def main(argv: "list[str] | None" = None) -> int:
    # ⛔ try_utf8_stdout() 要在 argparse 之前：`--help` 裡的 CJK 在 legacy Windows
    # console（cp950/cp936）會在 argparse 印出來之前就 UnicodeEncodeError。
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=i18n_text(
            "依 coverage 實測，列出 coverage source 內每個模組是被 in-process、"
            "subprocess、兩者、或都沒有執行到。",
            "Classify every module inside the coverage source by the kind of "
            "entry point that actually executed it, from real coverage data: "
            "in-process, subprocess, both, or neither.",
        ),
    )
    ap.add_argument("--repo", default=str(_REPO_ROOT),
                    help=i18n_text("要掃描的 repo 根目錄", "repository root to scan"))
    ap.add_argument("--coverage-data", default=None,
                    help=i18n_text("coverage 資料檔（預設：<repo>/.coverage）",
                                   "coverage data file (default: <repo>/.coverage)"))
    ap.add_argument("--json", action="store_true",
                    help=i18n_text("輸出 JSON", "emit JSON"))
    args = ap.parse_args(argv)

    def unmeasurable(reason: str) -> int:
        """rc 2 的唯一出口：診斷走 stderr，stdout **完全不寫**——`--json` 時也一樣。

        ⛔ 不要「順手」在這裡吐一份 JSON 錯誤文件。本 repo 的 `--json` 契約對這支
        工具釘的是相反的方向：`test_dx_json_stdout_contract` 的 `not-a-git-repo`
        recipe 要求 rc 2 時 stdout 為空，理由寫在那裡——**「量不到」不得偽裝成一份
        空的 JSON 清單**。下游看到的是 rc 2 + 空 stdout，那兩件事一起才是明確的。
        """
        print(f"[subproc-only] ⛔ 量不到：{reason}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        return unmeasurable(f"{repo} 不是 git repo")
    data_path = Path(args.coverage_data) if args.coverage_data else repo / ".coverage"
    try:
        data = build(repo, data_path)
    except (RuntimeError, subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        return unmeasurable(str(exc))

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"[subproc-only] coverage source: {', '.join(data['sources'])}")
    print(f"  coverage 資料檔              : {data['coverage_data']}")
    print(f"  模組（檔案）                 : {data['modules']}")
    print()
    print(f"  只被 subprocess 執行到       : {len(data['subprocess_only'])}")
    print(f"  兩種進入點都有               : {len(data['both'])}")
    print(f"  只被 in-process 執行到       : {len(data['in_process_only'])}")
    print(f"  ⚠️ 整輪都沒被執行到          : {len(data['unexecuted'])}")
    print()
    print("  ⚠️「只被 subprocess 執行到」不是盲點：conftest 接上 subprocess coverage")
    print("     之後那些模組是被量到的。它是進入點型態，不是量測缺口。")
    print("  ⚠️「整輪都沒被執行到」合併了「真的沒測試」與「有測試但沒被記錄」——")
    print("     從 coverage 資料分不出來，見本工具 docstring。")
    print()
    for module in data["subprocess_only"]:
        print(f"    {module}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
