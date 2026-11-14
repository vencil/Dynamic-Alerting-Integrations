"""`docs/assets/dist/` 不在 git 裡，且 `.gitignore` 蓋住它（#3738）。

為什麼
------
`docs/assets/dist/` 是 `tools/portal/` 的 build 產物。#3738 之前它被 commit，
所有消費端都信那份 commit 的副本，而那份副本在 main 上壞過：CI 在 PR 的 merge
ref 上比對過它，但 branch protection 不是 strict，main 在比對與 merge 之間移動，
一顆 import 了已不存在 chunk 的 bundle 就這樣部署到 Pages。修法分兩半：

* 每個消費端自己 build —— `tests/ops/test_portal_dist_consumers_build.py` 守。
* 目錄本身不進 git —— 本檔守。原本比對「commit 的 dist 是否跟得上 source」的
  pre-commit hook 與 CI drift gate 已隨之刪除，所以一旦有人 `git add -f` 把 dist
  加回來，**沒有任何東西會再比對它**，而讀 repo 的人（或日後新增、忘了先 build
  的消費端）會拿到一份無人看守的副本。這正是 #3738 要消滅的形狀。

斷言兩件事，各自獨立：

1. index 裡沒有任何 `docs/assets/dist/` 底下的路徑（`git ls-files --cached`，
   CI 與 pre-commit 看到的就是 index）。
2. repo 根目錄的 `.gitignore`（不是使用者的 global excludes）有一條規則讓該目錄
   底下的新檔被忽略——否則全新 clone 跑一次 `make portal-build`，`git add -A`
   就會把整包 bundle 帶進 commit，第 1 條要到那時才紅。

第三格把前兩格的路徑字面值綁到 `tools/portal/build.mjs` 實際的輸出目錄。

只呼叫 git，不讀網路、不跑 node。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST = "docs/assets/dist"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def test_no_portal_bundle_is_tracked() -> None:
    proc = _git("ls-files", "--cached", "-z", "--", DIST)
    # fail-closed：git 跑不起來時不能讀成「沒有 tracked 檔」。
    assert proc.returncode == 0, f"git ls-files failed (rc={proc.returncode}): {proc.stderr}"
    tracked = [p for p in proc.stdout.split("\0") if p]
    assert not tracked, (
        f"{len(tracked)} file(s) under {DIST}/ are in the git index, e.g. {tracked[:3]}. "
        "dist is build output of tools/portal/ and every consumer builds it itself "
        "(#3738); nothing compares a committed copy against source any more. "
        f"Run `git rm -r --cached {DIST}` and commit only the source."
    )


def test_root_gitignore_covers_the_dist_dir() -> None:
    # 用不存在的探針路徑 + --no-index：問的是「規則會不會忽略這裡的新檔」，
    # 與目錄此刻有沒有東西、有沒有被 track 無關。含一層子目錄，避免只蓋到
    # `*.js` 之類的檔名規則而漏掉 build 日後輸出的其他形狀。
    probes = [f"{DIST}/zz-probe-entry.js", f"{DIST}/zz-probe-entry.js.map",
              f"{DIST}/zz-probe-sub/chunk-ZZPROBE.js"]
    for probe in probes:
        # 「有沒有被忽略」看 `-q` 的 rc（`-v` 的輸出另外拿來問規則出處）。
        # 之後若有人加 `!/docs/assets/dist/` 把目錄重新納入，這裡是 rc 1。
        proc = _git("check-ignore", "--no-index", "-q", "--", probe)
        # rc 0 = 被忽略；1 = 沒被忽略；其餘（128 等）= git 本身失敗，不能讀成 1。
        assert proc.returncode in (0, 1), (
            f"git check-ignore failed (rc={proc.returncode}): {proc.stderr}")
        assert proc.returncode == 0, (
            f"{probe} is not ignored. Add `/{DIST}/` to the repo-root .gitignore "
            "(and no `!` rule re-including it) — otherwise `make portal-build` + "
            "`git add -A` commits the bundles (#3738).")
        # `-v` 印出 `<source>:<line>:<pattern>\t<path>`，即最後生效的那條規則。
        # 要求它來自 repo 根目錄的 .gitignore：開發者的 global excludes 或
        # .git/info/exclude 讓本機變綠，但 CI 與別人的 clone 沒有它。
        verbose = _git("check-ignore", "--no-index", "-v", "--", probe)
        assert verbose.returncode == 0, (
            f"git check-ignore -v disagrees with -q (rc={verbose.returncode}): "
            f"{verbose.stderr}")
        source = verbose.stdout.split(":", 1)[0]
        assert source == ".gitignore", (
            f"{probe} is ignored by {source!r}, not by the repo-root .gitignore; "
            "that rule does not travel with the repository.\n" + verbose.stdout)


def test_dist_is_where_the_build_writes() -> None:
    """上面兩格的 `DIST` 是字面值；若 build.mjs 把輸出搬到別處，它們會守著一個
    空目錄而照樣綠。這格把字面值綁到 build.mjs 實際寫入的 `DIST_DIR`。"""
    build = (REPO_ROOT / "tools" / "portal" / "build.mjs").read_text(encoding="utf-8")
    want = "const DIST_DIR = resolve(REPO_ROOT, " + ", ".join(
        f"'{part}'" for part in DIST.split("/")) + ");"
    assert want in build, (
        f"tools/portal/build.mjs no longer writes to {DIST}/ (expected `{want}`). "
        "Move DIST in this test, the .gitignore line and the consumers "
        "(tests/ops/test_portal_dist_consumers_build.py) together.")
