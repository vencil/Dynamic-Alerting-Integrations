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

掃描面（⚠️ 兩個都可能「量不到」，工具會說出來）
-----------------------------------------------
- ``commits`` —— `git log` 的訊息與 trailer。**shallow clone 只看得到一部分**，
  這種情況下「沒找到違規」不等於「沒有違規」，報告會標 ``PARTIAL``。
- ``titles`` —— issue / PR 標題，需要 ``GH_TOKEN``。⛔ 拿不到就 **rc 2**，
  絕不當成掃過了。（GitHub 的 search API 對本 token 回 403 ⇒ 改用列 issue
  再本地過濾。）

⛔ **兩個面不可互相取代，而且 commits 面比想像中弱得多**。在本 repo 的 shallow
clone 上實測（掃描面 = subject + trailer）：把 #1627 的六列拿掉之後，**commits
面一個都找不到、回綠**；六個全部只由 titles 面看得到（TRK-363←#1545、365←#1558、
366←#1564、367←#1571、368←#1574、380←#1757）。⇒ **只跑 commits 面的 pre-commit
hook 對這一類是弱後備，不是偵測器**；真正的偵測要 titles 面（需 token，見上）。

Usage
-----
::

    python3 scripts/tools/lint/check_trk_index_coverage.py                      # commits 面
    python3 scripts/tools/lint/check_trk_index_coverage.py --surface all --ci   # 全量（需 GH_TOKEN）
    python3 scripts/tools/lint/check_trk_index_coverage.py --json

Exit codes
----------
- ``0`` — 掃過了，被引用的 TRK 都在表上（**量了沒事**；若 ``PARTIAL`` 另見警告）
- ``1`` — 有 TRK 被引用卻不在表上（``--ci``）
- ``2`` — **量不到**：表解析不出來／表是空的／要求了 titles 面卻拿不到。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MAPPING = Path("docs/internal/planning-id-mapping.md")

# (?<![\w-]) / (?![\w-]) 讓 TRK-3811 與 xTRK-381 都不會誤配
_TRK_RE = re.compile(r"(?<![\w-])TRK-(\d{3})(?![\w-])")
_TRK_CELL_RE = re.compile(r"^TRK-(\d{3})$")
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


# ⛔ 掃的是 **subject 行 + trailer**，不是整篇 commit body。
# 票寫的掃描面是「issue／PR 標題與 commit **trailer**」。掃整個 body 會把散文裡
# 當例子提到的號碼讀成一次引用——實測：本工具自己的 commit 訊息描述 dogfood 時
# 寫了 `TRK-999`／`TRK-401`（fixture 用的假號），整個 body 掃法把它們判成兩個洞，
# 於是**守衛擋下了自己的落地 commit**。那是誤紅，不是缺陷。
# trailer 用 git 原生 parser 取（與 check_planning_status_sync.py 同一理由：
# RFC-2822 的大小寫、空行分隔、多行值，regex 打不準）。
_TRAILER_KEYS = ("Refs", "Resolves", "Closes", "Fixes", "Fix", "Related")


def commit_trks(repo: Path, limit: int = 2000) -> tuple[dict[str, str], bool]:
    """commit 的 subject + trailer 裡的 TRK。回傳 (trk -> 短 SHA, 是否完整歷史)。"""
    shallow = (repo / ".git" / "shallow").exists()
    keys = ",".join(f"key={k},valueonly=true,unfold=true" for k in _TRAILER_KEYS)
    fmt = "%h%x00%s%x00%(trailers:" + keys + ")%x1e"
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", f"-n{limit}", f"--format={fmt}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return {}, False
    hits: dict[str, str] = {}
    for rec in proc.stdout.split("\x1e"):
        parts = rec.split("\x00")
        if len(parts) < 3:
            continue
        sha, subject, trailers = parts[0].strip(), parts[1], parts[2]
        for m in _TRK_RE.finditer(subject + "\n" + trailers):
            hits.setdefault(m.group(1), sha)
    return hits, not shallow


def title_trks(owner: str, repo: str, token: str) -> dict[str, str]:
    """issue / PR 標題裡的 TRK。⛔ search API 對本 token 回 403 ⇒ 列 issue 本地過濾。"""
    if not (_SLUG_RE.match(owner) and _SLUG_RE.match(repo)):
        raise ValueError(f"owner/repo 不是合法的 GitHub slug: {owner!r}/{repo!r}")
    hits: dict[str, str] = {}
    page = 1
    while page <= 40:
        url = f"{_API.format(owner=owner, repo=repo)}?state=all&per_page=100&page={page}"
        # 述詞而不是註解：host 與 scheme 是常數前綴，argv 只能影響其後的路徑段，
        # 而那兩段已由 _SLUG_RE 收斂過。
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
        if not batch:
            break
        for it in batch:
            for m in _TRK_RE.finditer(it.get("title") or ""):
                hits.setdefault(m.group(1), f"#{it['number']}")
        page += 1
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--surface", default="commits", choices=["commits", "titles", "all"])
    ap.add_argument("--ci", action="store_true", help="有違規時 exit 1")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo", default=str(_REPO_ROOT))
    ap.add_argument("--owner", default="vencil")
    ap.add_argument("--gh-repo", default="dynamic-alerting-integrations")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / _MAPPING).exists():
        print(f"[trk-index] ⛔ 量不到：找不到 {_MAPPING}", file=sys.stderr)
        return 2

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
    partial = False

    if args.surface in ("commits", "all"):
        hits, complete = commit_trks(repo)
        used.update(hits)
        surfaces.append(f"commits({len(hits)}{'' if complete else ', PARTIAL/shallow'})")
        partial = partial or not complete

    if args.surface in ("titles", "all"):
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
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            print(f"[trk-index] ⛔ 量不到：issue 標題抓不到（{exc}）", file=sys.stderr)
            return 2
        for k, v in hits.items():
            used.setdefault(k, v)
        surfaces.append(f"titles({len(hits)})")

    missing = sorted(n for n in used if n not in defined)

    if args.json:
        print(json.dumps(
            {"defined": len(defined), "used": len(used), "surfaces": surfaces,
             "partial": partial,
             "missing": [{"trk": n, "seen_at": used[n]} for n in missing]},
            ensure_ascii=False, indent=2))
    else:
        print(f"[trk-index] 表上定義 {len(defined)} 個 TRK；掃描面：{', '.join(surfaces)}")
        if partial:
            print("[trk-index] ⚠️ PARTIAL：shallow clone，commit 面只看得到一部分 —— "
                  "「沒找到」不等於「沒有」。完整結果請在有完整歷史處或加 --surface all 重跑。")
        if missing:
            for n in missing:
                print(f"  ✗ TRK-{n} 被引用（{used[n]}）卻不在 {_MAPPING} 的索引裡")
            print(f"[trk-index] 共 {len(missing)} 個洞。補列或明記「此號未使用」都可以。")
        else:
            print("[trk-index] ✅ 量了沒事：被引用的 TRK 都在表上")

    if missing and args.ci:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
