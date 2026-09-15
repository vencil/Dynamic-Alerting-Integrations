---
name: vibe-workflow
description: Vibe session 起手式 + 最常踩的 7 個坑 + 標準開發 session 工作流。Use at the start of any Vibe working session (especially first Bash/Edit/Write call), when encountering FUSE phantom lock / stale git index / docker exec returning empty stdout / port-forward residue / pre-commit lock artifacts / ad-hoc script rejection, or when planning the end-to-end flow from code change through commit to PR. Also use when the user mentions "起手式", "FUSE 卡住", "docker exec 沒輸出", "win-commit", or when orienting to how Vibe's dev loop is supposed to run.
---

# vibe-workflow

## 起手式

先照 [CLAUDE.md §起手式](../../../CLAUDE.md) 量 hook 有沒有跑（`cat /tmp/vibe-session-start-hook.ran`），沒有就手動跑 `session-start.sh`。本機 session 的 PreToolUse hook 第一次 `Bash`/`Write`/`Edit` 時跑 `scripts/session-guards/session-init.py`（關 VS Code Git 背景操作、寫 session marker）；web 多 repo session 這支不會被載入（#1719）。

- 手動觸發／偵錯：`python scripts/session-guards/session-init.py [--status|--force|--stats]`
- Telemetry：`~/.cache/vibe/session-init.log`（Windows：`%LOCALAPPDATA%\vibe\session-init.log`）；`VIBE_SESSION_LOG=/dev/null` 停用
- Dev Container（K8s / Go test / Helm）：`docker start vibe-dev-container`、`make dc-up`、`make dc-test`
- Session 結束：`make session-cleanup`

主路徑是 Dev Container 做所有事（`make dc-run CMD="..."` / `make dc-test` / `make dc-go-test`）；逃生門是 FUSE 卡死時用 Windows 原生 git（`scripts/ops/win_git_escape.bat` 或 `make win-commit`）。

## 七個坑與救援指令

1. ⛔ **`sed -i`** — 改用 Read+Edit；批次替換走 pipe：`sed '...' < file > file.tmp && mv file.tmp file`。`preflight_bash.py` 會攔掛載路徑上的 `sed -i`（web session 除外）。
2. **FUSE phantom lock** → `make git-preflight`（或 `make git-lock ARGS="--clean"`）；頑強殘影 `make fuse-reset`（Level 2/4/5 見 [windows-mcp-playbook §修復層 B](../../../docs/internal/windows-mcp-playbook.md#修復層-bfuse-cache-重建level-1--5)）；反覆卡住走 Windows 逃生門（[§修復層 C](../../../docs/internal/windows-mcp-playbook.md#修復層-cwindows-原生-git-fallbackfuse-側卡死時的備援路徑)）。
   ⛔ 不要用 FUSE temp index（`GIT_INDEX_FILE=/tmp/xxx`）commit：FUSE 側 `.git/index` 永遠 stale，`commit-tree` 產出的 tree 不含修改。git add/commit/push 從 Windows 側執行：`make win-commit MSG=_msg.txt FILES="a b"`。
3. **docker exec stdout 為空** → 重導向 `> /workspaces/.../_out.txt 2>&1` 再 `cat`，或 `make dc-run CMD="..."`（[§核心原則](../../../docs/internal/windows-mcp-playbook.md)）。
4. **pre-commit 中斷留下 .git lock** → `make git-lock ARGS="--clean"`，不要 `--no-verify`。
5. **port-forward 殘留佔用端口** → `pkill -f "port-forward.*prometheus"` 或 `make session-cleanup`。
6. ⛔ **不寫 `_foo.bat` / `_p*_commit.ps1` throw-away script** — `check_ad_hoc_git_scripts` 會擋。GitHub CLI 用 `scripts/ops/win_gh.bat`（`pr-checks`/`pr-view`/`pr-create`/`run-view`/`run-log`/`raw`），git 用 `scripts/ops/win_git_escape.bat`（`status`/`add`/`commit-file`/`push`/`preflight`）；缺子命令就擴充 wrapper（[LL #54](../../../docs/internal/windows-mcp-playbook.md#已知陷阱速查)）。
7. **UTF-8 commit message 亂碼**（cmd.exe codepage）→ `make win-commit` 或 `python scripts/ops/commit_helper.py commit-file <msg>`（[LL #58](../../../docs/internal/windows-mcp-playbook.md#已知陷阱速查)）。

## 從改動到 PR

1. 依任務類型讀對應 Playbook（`vibe-playbook-nav`）。
2. 改碼 → Go test / Python test → 場景驗證（`make dc-*`）。
3. 效能相關變更跑完整 benchmark（idle + routing + Go micro-bench），記到 CHANGELOG 與 architecture docs。
4. 文件同步：`make version-check` 只檢查；要更新計數跑 `python3 scripts/tools/dx/bump_docs.py --sync-counts`（`make bump-docs` 是版號 bump，不做計數）。沒有 pre-commit hook 跑 bump_docs。
5. `git commit`（FUSE 卡住時 `make win-commit`）。
6. `make pr-preflight`（寫 `.git/.preflight-ok.<SHA>` marker）→ `gh pr create`。
7. 新陷阱回寫對應 Playbook；跨 session 高頻才升到 CLAUDE.md 或本 skill。
