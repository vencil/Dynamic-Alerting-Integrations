#!/usr/bin/env python3
"""check_trk_index_coverage.py — 被引用的 TRK 必須出現在 planning SSOT 的索引裡。

Why this exists
---------------
`docs/internal/planning-id-mapping.md` 是 `CLAUDE.md` 指定的 Planning / Tracking
ID **對照 SSOT**，[ADR-019](../../../docs/adr/019-planning-ssot.md) 把 `TRK-NNN`
定為唯一新進入點。但表上有洞而號碼在外面被用，會有三個直接後果
（[#1627](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1627)）：

1. **配號會撞** —— 下一個人看表以為某號是空的。這條線**已經因此吵過兩次號碼**
   （TRK-372、TRK-379 都要裁決改號）。
2. **以 TRK 為軸的查詢會漏** —— `grep TRK-367` 在表上找不到，而 commit 歷史裡有。
3. **它是靜默的** —— 在本工具之前，沒有任何檢查在驗這件事。

⚠️ 這不是整理癖，是止血：開票時的洞是 363–368，實測**集合已經換了人**（364 補上、
380 是新的），而票**標題裡的數字自己已經漂了**。

述詞
----
被引用的 TRK 集合 **⊆** 表上定義的 TRK 集合。判準是**存在性**，不判語意——
PR 引用它所實作那張票的 TRK、子票引用親代 TRK 都是正確用法，不在本工具射程內
（#1627 明寫那一半「大量誤紅」，補列的人不需要重跑）。

⛔ 表上「定義」的認定是**逐列拆欄**，不是全文 grep：只看表格列的**前兩欄**
（`TRK-300+` 區段 TRK 在第 1 欄，legacy `HA-N` / `REG-NNN` / `TD-NNN` 三個區段
TRK 在第 2 欄）。散文裡提到某個 TRK **不算**它被定義了——本工具第一版就是因為
只讀第 1 欄而少算了 legacy 三段、跑出五個假洞。

掃描面：issue / PR 標題
-----------------------
由 ``.github/workflows/trk-index-coverage.yaml`` 跑（#2106）：issue／PR 事件只驗
觸發它的那一個標題（``--title``，不需 token），``workflow_dispatch`` 才全量掃
（``--all-titles``，需要 ``GH_TOKEN``；⛔ 拿不到就 **rc 2**，絕不當成掃過了。
GitHub 的 search API 對本 token 回 403 ⇒ 改用列 issue 再本地過濾）。表本身
（凍結、解析）每次都驗，不需要任何掃描面。

⛔ **不掃 commit 歷史**——這是刪掉的，不是遺漏。舊版有一個 ``commits`` 面，對本機
歷史跑 ``git log -n2000 --format=%(trailers:…)``，三個毛病疊在一起：

1. **讀的是衍生物**：main 上的 squash 訊息會被 GitHub 在空行後附加
   ``Co-authored-by:``，把 trailer 推出 block；`dev-rules.md` §P1（#1741）已規定
   main 上不以 ``%(trailers)`` 讀。實測它因此在最近 2000 顆 commit 上少讀 15 個 TRK。
2. **看不到要擋的那一顆**：它只掛在 pre-commit 的 ``pre-commit`` 階段，那時新
   commit 的訊息還不存在；CI 也沒有跑它。把 #1627 的六列拿掉實測 **0/6、回綠**，
   六個全部只由標題面看得到（TRK-363←#1545、365←#1558、366←#1564、367←#1571、
   368←#1574、380←#1757）。
3. **一次失誤就全 repo 鎖死**：違規一旦進了 main，補列會違反凍結、改寫 main 又
   被禁止，於是每個人每次 commit 永久紅。

表凍結後新號碼一律是四位數的 issue 號，三位數新號只可能是手配，而手配的號碼會
出現在 issue／PR 標題上——那一面才是偵測器。

⛔ 只認**三位數**號碼是刻意的，不是遺漏：表凍結在 TRK-392，新條目改用
``TRK-<issue 號>``（ADR-019，#2106），那些號碼本身就指向 issue、不需要登記。所以
表上沒有的三位數新號＝有人照舊看表手配號，正是要擋的東西；表上出現凍結時
沒有的號碼（TRK-392 以後、四位數、或補 legacy 區段的洞）同樣算違規（手配號的
人會順手補列，讓自己的引用「在表上」）。

Usage
-----
::

    python3 scripts/tools/lint/check_trk_index_coverage.py --ci                       # 只驗表（凍結、解析）
    python3 scripts/tools/lint/check_trk_index_coverage.py --ci --title "TRK-393: x"  # 再驗一個標題
    python3 scripts/tools/lint/check_trk_index_coverage.py --ci --all-titles          # 全部標題（需 GH_TOKEN）
    python3 scripts/tools/lint/check_trk_index_coverage.py --json

Exit codes
----------
- ``0`` — 掃過了，被引用的 TRK 都在表上（**量了沒事**）
- ``1`` — 有 TRK 被引用卻不在表上，或表上出現凍結時沒有的號碼（``--ci``）
- ``2`` — **量不到**：表解析不出來／表是空的／要求了 titles 面卻拿不到。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR, Report  # noqa: E402
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402
from _lib_prometheus import TRANSPORT_ERRORS, transport_error_text  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAPPING = Path("docs/internal/planning-id-mapping.md")

# (?<![\w-]) / (?![\w-]) 讓 TRK-3811 與 xTRK-381 都不會誤配
_TRK_RE = re.compile(r"(?<![\w-])TRK-(\d{3})(?![\w-])")
_TRK_CELL_RE = re.compile(r"^TRK-(\d{3,})$")  # 四位數的列也要讀到，才擋得下
_API_HOST = "https://api.github.com/"
_API = _API_HOST + "repos/{owner}/{repo}/issues"
# owner / repo 由 argv 給，先收斂成 GitHub 實際允許的字元集，避免它們把路徑帶去別處。
# ⚠️ 字元集本身不夠：`.` 與 `..` 都通過字元集卻是路徑走訪，實測會讓請求真的發出去
# （測試那格打到 api.github.com 收 403 才發現）。所以另外排除純點的段。
_SLUG_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9._-]+$")


def defined_trks(repo: Path) -> set[str]:
    """表上定義的 TRK：逐列拆欄，只認前兩欄的獨立 ``TRK-NNN`` 儲存格。"""
    fp = repo / _MAPPING
    out: set[str] = set()
    for line in fp.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for cell in cells[:2]:
            m = _TRK_CELL_RE.match(cell)
            if m:
                out.add(m.group(1))
    return out


class TitlesTruncated(RuntimeError):
    """翻到頁數上限仍有下一頁：沒翻到的標題不能當成掃過了。"""


_MAX_PAGES = 40
# 表凍結時的號碼（ADR-019，#2106）：之後只許少、不許多。新條目用 TRK-<issue 號>，
# 不再登記。⛔ 用集合不用上限：legacy 區段有洞，「補洞」一樣是照舊手配號。
_FROZEN = "001-018,101,103-104,201-203,205-212,216-222,224,226,228-240,242,300-392"
_FROZEN_SET = frozenset(
    n for part in _FROZEN.split(",")
    for n in range(int(part.partition("-")[0]), int(part.partition("-")[2] or part) + 1))
_FROZEN_MAX = max(_FROZEN_SET)
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def title_trks(owner: str, repo: str, token: str) -> dict[str, str]:
    """issue / PR 標題裡的 TRK。⛔ search API 對本 token 回 403 ⇒ 列 issue 本地過濾。"""
    if not (_SLUG_RE.match(owner) and _SLUG_RE.match(repo)):
        raise ValueError(f"owner/repo 不是合法的 GitHub slug: {owner!r}/{repo!r}")
    hits: dict[str, str] = {}
    # ⛔ 跟 Link: rel="next" 走 cursor 分頁，不自己拼 `page=N`：issues API 對大資料集
    # 拒收頁碼式分頁（匿名請求第 11 頁起 422，訊息要求改用 after/before）。
    # ⚠️ 這個端點會**靜默省略**token 讀不到的 PR：沒有 pull-requests: read 的
    # GITHUB_TOKEN 只拿到 issue，rc 照樣 0/1（#2106 實測少 6 個只出現在 PR 標題的 TRK）。
    # 工具分辨不出來，權限由呼叫端（workflow）負責給齊。
    url = f"{_API.format(owner=owner, repo=repo)}?state=all&per_page=100"
    pages = 0
    while url:
        # ⛔ 上限只防無窮迴圈；撞到上限時剩下的（最舊的）標題沒被看過，要回 rc 2。
        if pages >= _MAX_PAGES:
            raise TitlesTruncated(f"翻了 {_MAX_PAGES} 頁仍有下一頁")
        # 述詞而不是註解：第一頁的 host 是常數前綴、路徑段已由 _SLUG_RE 收斂；
        # 之後的 URL 來自回應的 Link header，同樣只准回到 _API_HOST。
        if not url.startswith(_API_HOST):
            raise ValueError(f"refusing to fetch a non-GitHub URL: {url!r}")
        req = urllib.request.Request(  # nosec B310  # https-only, host pinned to _API_HOST above
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "check_trk_index_coverage",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310  # same, scheme+host asserted
            batch = json.load(resp)
            m = _NEXT_RE.search(resp.headers.get("Link") or "")
        pages += 1
        # 200 但不是 issue 物件的陣列（dict、null、數字陣列…）：量不到，不是「沒有 TRK」。
        if not isinstance(batch, list) or not all(isinstance(it, dict) for it in batch):
            raise ValueError("issues API 回的不是物件陣列")
        for it in batch:
            title = it.get("title") or ""
            if not isinstance(title, str):
                raise ValueError("issues API 回的 title 不是字串")
            for hit in _TRK_RE.finditer(title):
                hits.setdefault(hit.group(1), f"#{it.get('number', '?')}")
        url = m.group(1) if m else ""
    return hits


def main(argv: list[str] | None = None) -> "int | Report":
    # ⛔ try_utf8_stdout() 要在 argparse 之前：`--help` 裡的 CJK 在 legacy Windows
    # console（cp950/cp936）會在 argparse 印出來之前就 UnicodeEncodeError。
    try_utf8_stdout()
    ap = argparse.ArgumentParser(
        description=i18n_text(
            "斷言被引用的 TRK 都出現在 planning SSOT 的索引裡——"
            "表上有洞而號碼在外面被用，會讓下一個人配號撞號，而且是靜默的。",
            "Assert that every referenced TRK appears in the planning SSOT "
            "index. A hole in the table while the number is in use makes the "
            "next person collide on it, silently.",
        ),
    )
    ap.add_argument("--all-titles", action="store_true",
                    help=i18n_text("掃全部 issue／PR 標題（需要 GH_TOKEN）",
                                   "scan every issue/PR title (needs GH_TOKEN)"))
    ap.add_argument("--ci", action="store_true",
                    help=i18n_text("有違規時 exit 1", "exit 1 when violations are found"))
    ap.add_argument("--json", action="store_true",
                    help=i18n_text("輸出 JSON", "emit JSON"))
    ap.add_argument("--repo", default=str(_REPO_ROOT),
                    help=i18n_text("要掃描的 repo 根目錄", "repository root to scan"))
    ap.add_argument("--owner", default="vencil",
                    help=i18n_text("GitHub owner", "GitHub owner"))
    ap.add_argument("--gh-repo", default="dynamic-alerting-integrations",
                    help=i18n_text("GitHub repo 名", "GitHub repo name"))
    ap.add_argument("--title", default=None,
                    help=i18n_text("只驗這一個標題（titles 面，不需 token；workflow 由事件 payload 傳入）",
                                   "check only this one title (titles surface, no token needed)"))
    ap.add_argument("--title-ref", default="(--title)",
                    help=i18n_text("--title 的出處（如 #2150），用於報告", "where --title came from, for the report"))
    args = ap.parse_args(argv)
    if args.title is not None and args.all_titles:
        ap.error(i18n_text("--title 與 --all-titles 只能擇一",
                           "--title and --all-titles are mutually exclusive"))

    repo = Path(args.repo).resolve()
    if not (repo / _MAPPING).exists():
        print(f"[trk-index] ⛔ 量不到：找不到 {_MAPPING}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    defined = defined_trks(repo)
    if not defined:
        print(
            "[trk-index] ⛔ 量不到：表上解析出 0 個 TRK。"
            "解析壞掉與「表是空的」長得一樣，所以這裡不回 0。",
            file=sys.stderr,
        )
        return 2

    used: dict[str, str] = {}
    surfaces: list[str] = []

    if args.title is not None:
        hits = {m.group(1): args.title_ref for m in _TRK_RE.finditer(args.title)}
        used.update(hits)
        surfaces.append(f"title {args.title_ref}({len(hits)})")
    elif args.all_titles:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            print(
                "[trk-index] ⛔ 量不到：要求了 titles 掃描面但沒有 GH_TOKEN／GITHUB_TOKEN。"
                "不會當成掃過了。",
                file=sys.stderr,
            )
            return 2
        try:
            hits = title_trks(args.owner, args.gh_repo, token)
        # ValueError：slug 不合法、Link 指向別的 host、JSON 壞掉——都是量不到，不是違規。
        # 傳輸失敗只列 TRANSPORT_ERRORS（#3519）：回應不是 HTTP 也是量不到。
        except (*TRANSPORT_ERRORS, TitlesTruncated) as exc:
            print(f"[trk-index] ⛔ 量不到：issue 標題抓不到（{transport_error_text(exc)}）",
                  file=sys.stderr)
            return 2
        for k, v in hits.items():
            used.setdefault(k, v)
        surfaces.append(f"titles({len(hits)})")

    missing = sorted(n for n in used if n not in defined)
    # ⛔ 凍結要有機制，不能只是散文：照舊看表取號的人會順手補一列，那一列會讓
    # 它自己的引用「在表上」而轉綠（#2106 盲審實測，含補 legacy 區段的洞）。
    past_freeze = sorted((n for n in defined if int(n) not in _FROZEN_SET), key=int)

    status = 1 if (missing or past_freeze) and args.ci else 0
    if args.json:
        return Report(
            {"defined": len(defined), "used": len(used), "surfaces": surfaces,
             "missing": [{"trk": n, "seen_at": used[n]} for n in missing],
             "past_freeze": past_freeze},
            status)  # #3216: run() encodes it
    else:
        print(f"[trk-index] 表上定義 {len(defined)} 個 TRK；"
              f"掃描面：{', '.join(surfaces) or '（只驗表）'}")
        for n in past_freeze:
            print(f"  ✗ TRK-{n} 被登記在 {_MAPPING}，但表已凍結（到 TRK-{_FROZEN_MAX}）："
                  "新條目改用 TRK-<issue 號>，不要再加列或補洞")
        for n in missing:
            print(f"  ✗ TRK-{n} 被引用（{used[n]}）卻不在 {_MAPPING} 的索引裡 → "
                  "三位數屬於凍結的舊表，新條目改用四位數以上的新追蹤 issue 號")
        if missing or past_freeze:
            print(f"[trk-index] 共 {len(missing) + len(past_freeze)} 個問題。")
        else:
            print("[trk-index] ✅ 量了沒事：被引用的 TRK 都在表上")

    return status


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)  # #3216: run() encodes the --json document
