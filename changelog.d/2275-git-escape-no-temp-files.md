---
section: Fixed
topic: dev-workflow
issues: [2275]
created: 2026-09-28T23:59:00+08:00
---
- **Windows 逃生門 `win_git_escape.bat` 同時跑兩個呼叫不再互搶輸出（internal、dx；[#2275](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2275)）**：以前每個 git 呼叫都寫到固定的 `%TEMP%\vibe-git-out.txt`／`vibe-git-err.txt`，兩棵 worktree 同時用逃生門時一方會失敗，`FAILED:` 也可能印出另一個呼叫的輸出。現在不落檔，git 的輸出（含錯誤）直接印在 stdout。`log`／`diff`／`branch` 不會停在 pager。`win_git_escape.bat`、`win_gh.bat` 檔頭與 `windows-mcp-playbook.md` 的 MCP 呼叫樣板改成每次呼叫用一個帶 GUID 的輸出檔，讀完即刪。
