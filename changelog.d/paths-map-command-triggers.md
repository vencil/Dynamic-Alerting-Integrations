---
section: Added
topic: dx
issues: []
created: 2026-09-27T01:30:00+08:00
---
- **paths-map 也能以 Bash 指令觸發提醒（dx）**：`.agents/paths-map.json` 新增頂層 `commands`，每列是一條對整條指令比對的正規式；`paths_map.py` 在 Bash 執行前比對，命中就注入「先讀哪一節＋一句約束」，每列每個 session 一次。先掛三個沒有路徑可比對的時點：rebase／merge／cherry-pick、刪分支或 worktree（先比對 PR 的 `headRefOid`）、`gh pr create`（查 `closingIssuesReferences` 與 CodeRabbit 三端點）。每列必須附會被自己命中的 `examples`，寫錯的正規式會讓 `--validate` 失敗；只有 Claude Code 讀這一半。
