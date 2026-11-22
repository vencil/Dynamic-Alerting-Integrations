---
section: Fixed
topic: da-tools
issues: [3107]
created: 2026-11-15T07:10:35+00:00
---
- **重產 repo 生成物不再把檔案權限收成 0600（da-tools／repo 工具；[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：`check_frontmatter_versions --fix`、`threshold-recommend --generate-observed-map`、`check_threshold_registry --regen` 寫的是被 git 追蹤的檔，以前跑完會從 0644 變成 0600，而 `git status` 看不出來（git 只記執行位）。現在改走 `write_repo_artifact`：原子取代，權限只保留執行位（原檔可執行為 0755，其餘與新檔為 0644，不讀 umask）；明傳 `mode` 的呼叫者不變。⚠️ 行為變更：(1) 目標可寫但屬於別的使用者時 `2` → `0`（以前內容寫入後 chmod 失敗），改為原地寫入加兩行 WARN、權限不動；(2) 目標有多個 hard link、或所在目錄不可寫時改為原地寫入並多一行 WARN（結束碼仍 `0`）；(3) 目標是目錄時訊息由 `Is a directory (errno 21)` 改為 `not a regular file`；(4) 未傳 `mode` 的既有呼叫者（`generate_adr_index`、`generate_planning_index`）寫可執行檔時 0644 → 0755。目標不可寫、是目錄、父目錄不存在的結束碼不變。
