---
section: Fixed
topic: dev-workflow
issues: [2328]
created: 2026-09-30T01:00:00+08:00
---
- **`git_check_lock.sh --clean` 不再刪除 `.git` 的 lock，linked worktree 裡的 lock 也列得出來（internal、dx；[#2328](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2328)）**：以前它靠 `pgrep` 判斷有沒有 git 在跑，而 Git Bash 沒有 `pgrep`，判成「沒有」之後就把超過 30 秒的 lock 刪掉，包括正被 git 持有的；它又寫死 `<tree>/.git`，在 linked worktree（`.git` 是檔案）一把鎖都找不到卻回報一切正常。現在 lock 的位置由 `git rev-parse --git-common-dir` 推導，而且一律只列出路徑與手動刪除指令，不自動刪除（與 #1919 對 `win_git_escape.bat` 的裁定一致）。量不到活躍 git 時明說「無法判斷」。`--clean` 修復被 NUL 填滿的 `.git/HEAD` 照舊。`make git-preflight`、`make session-cleanup`、`make fuse-reset` 因此也只列出 lock、不再刪除。
