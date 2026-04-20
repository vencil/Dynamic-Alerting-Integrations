---
name: vibe-workflow
description: Vibe session 起手式 + 最常踩的 7 個坑 + 標準開發 session 工作流。Use at the start of any Vibe working session (especially first Bash/Edit/Write call), when encountering FUSE phantom lock / stale git index / docker exec returning empty stdout / port-forward residue / pre-commit lock artifacts / ad-hoc script rejection, or when planning the end-to-end flow from code change through commit to PR. Also use when the user mentions "起手式", "FUSE 卡住", "docker exec 沒輸出", "win-commit", or when orienting to how Vibe's dev loop is supposed to run.
---

# vibe-workflow — Session 起手式 + 7 個坑 + 標準工作流

## Session 起手式（已自動化 🛡️）

起手式已 codified 為 **PreToolUse hook**（v2.8.0）— 第一次 `Bash`/`Write`/`Edit`/`MultiEdit` 呼叫自動跑 `scripts/session-guards/session-init.py`（關 VS Code Git 背景操作 + 寫 session marker），後續同 session 呼叫 O(1) no-op。Session 用 `CLAUDE_SESSION_ID` 區分，marker 在 `/tmp/vibe-session-init.<hash>`。

- **手動觸發**（偵錯）：`python scripts/session-guards/session-init.py [--status|--force|--stats]`
- **Telemetry**（v2.8.0 Phase .b）：每次 hook 呼叫自動 append JSON Lines 到 `~/.cache/vibe/session-init.log`（Windows：`%LOCALAPPDATA%\vibe\session-init.log`）。用 `--stats` 印 counts + 最近事件；`--stats --json` 供 `jq` pipe；`--stats --session <SID>` 過濾；`VIBE_SESSION_LOG=/dev/null` 停用
- **Dev Container**（K8s / Go test / Helm）：`docker start vibe-dev-container`（或用 `make dc-up` / `make dc-test`）
- **Session 結束**：`make session-cleanup`

Hook 設定見 [`.claude/settings.json`](../../../.claude/settings.json)。完整原理見 [windows-mcp-playbook §FUSE Phantom Lock 防治](../../../docs/internal/windows-mcp-playbook.md#fuse-phantom-lock-防治)。

### 設計原則：主路徑 / 逃生門

> **主路徑**：Dev Container 層做所有事（code / test / commit / push）。用 `make dc-run CMD="..."` / `make dc-test` / `make dc-go-test` 統一入口。
> **逃生門**：FUSE 卡死時，用 Windows 原生 git 完成操作（`scripts/ops/win_git_escape.bat` 或 `make win-commit`）。
> **目標**：不讓任何 session 因 FUSE 問題整個卡死。

## 最常踩的 7 個坑

1. **⛔ 永遠不要用 Bash 工具執行 `sed -i`** — 改用 Read+Edit 工具。已有 shell wrapper 攔截（`vibe-sed-guard.sh`），違反時會直接報錯阻止。如需批次替換用 pipe：`sed '...' < file > file.tmp && mv file.tmp file`

2. **FUSE phantom lock** → `make git-preflight`（或 `make git-lock ARGS="--clean"`）；頑強殘影升級 `make fuse-reset`（Level 1+3 自動，Level 2/4/5 指引見 [windows-mcp-playbook §修復層 B](../../../docs/internal/windows-mcp-playbook.md#修復層-bfuse-cache-重建level-1-5)）。FUSE 側 git 操作反覆卡住時 → **Windows 逃生門**：`scripts/ops/win_git_escape.bat`（[§修復層 C](../../../docs/internal/windows-mcp-playbook.md#修復層-cwindows-原生-git-fallbackfuse-側卡死時的備援路徑)）

   **2b. ⛔ 不要用 FUSE temp index（`GIT_INDEX_FILE=/tmp/xxx`）做 git commit** — `.git/index` 在 FUSE 側永遠是 stale 的，`commit-tree` 產出的 tree 不含修改。**所有 git add/commit/push 必須從 Windows 側執行**：`make win-commit MSG=_msg.txt FILES="a b"`（hook-gated wrapper），或手動 `cd C:\Users\vencs\vibe-k8s-lab && git add ... && git commit --no-verify -F _msg.txt && git push --no-verify`

3. **docker exec stdout 為空** → 用 `> /workspaces/.../_out.txt 2>&1` 重導向再 `cat`，或直接用 `make dc-run CMD="..."`（已封裝此 pattern）。見 [windows-mcp-playbook §核心原則](../../../docs/internal/windows-mcp-playbook.md)

4. **pre-commit hook 中斷留下 .git lock** → `make git-lock ARGS="--clean"`，**不要** `--no-verify`

5. **port-forward 殘留佔用端口** → `pkill -f "port-forward.*prometheus"` 或 `make session-cleanup`

6. **⛔ 絕對不要寫 `_foo.bat` / `_p*_commit.ps1` 這類 throw-away script** — `check_ad_hoc_git_scripts` (L1 pre-commit) 會 whitelist block。需要 GitHub CLI 用 `scripts/ops/win_gh.bat`（`pr-checks`/`pr-view`/`pr-create`/`run-view`/`run-log`/`raw`），需要 git 用 `scripts/ops/win_git_escape.bat`（`status`/`add`/`commit-file`/`push`/`preflight` 等）。**缺子命令就擴充 wrapper，不寫 sibling script**。見 [windows-mcp-playbook LL #54](../../../docs/internal/windows-mcp-playbook.md#已知陷阱速查)

7. **UTF-8 commit message 亂碼**（cmd.exe codepage）→ 用 `make win-commit` 或 `python scripts/ops/commit_helper.py commit-file <msg>`（pipes bytes via `git commit -F -`，繞過 cmd.exe 重編碼）。見 [windows-mcp-playbook LL #58](../../../docs/internal/windows-mcp-playbook.md#已知陷阱速查)

## 標準開發 Session 工作流

1. **起手式**：PreToolUse hook 自動跑；任務開始前根據任務類型讀對應 Playbook（見 `vibe-playbook-nav` skill）
2. **開發**：程式碼修改 → Go test / Python test → 場景驗證（偏好 `make dc-*` 統一入口）
3. **Benchmark**（效能相關變更）：完整 benchmark（idle + routing + Go micro-bench）→ 記錄到 CHANGELOG + architecture docs
4. **文件同步**：`bump_docs.py --check` → 更新 CLAUDE.md / README / CHANGELOG 的計數（違反 Doc-as-Code #4 會被 hook 擋）
5. **Commit**：`git commit`（或 FUSE 卡住時 `make win-commit`）→ pre-commit hooks 自動執行品質檢查
6. **PR 收尾**：`make pr-preflight`（七項檢查 + 寫 `.git/.preflight-ok.<SHA>` marker）→ `gh pr create`
7. **Lesson Learned**：遇到新陷阱回寫對應 Playbook + 更新 `vibe-workflow` 或 `vibe-playbook-nav` skill

## 使用法

- Session 開始 → hook 自動跑，直接動手即可
- 遇到上述 7 種症狀 → 對照本文找到救援指令
- 新陷阱 → 優先回寫對應 Playbook，僅當跨 session 高頻才升級到 CLAUDE.md / 本 skill
