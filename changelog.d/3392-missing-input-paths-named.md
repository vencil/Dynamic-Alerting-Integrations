---
section: Changed
topic: da-tools
issues: [3392]
created: 2026-10-31T04:38:07+00:00
---
- **呼叫端給的輸入路徑不存在或型別錯時改為呼叫端錯誤，不再當成空輸入（ops 工具；[#3392](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3392)）**：**Breaking**，結束碼改為 2，在寫出任何東西之前點名旗標與路徑（`--json` reason 為 `*_not_found`／`*_not_accessible`）：operator-generate `--rule-packs-dir`／`--config-dir`、migrate-to-operator `--source-dir`／`--config-dir`、blind-spot `--config-dir`、shadow-verify `--report-csv`／`--readiness-json`、operator-check `--rule-packs-dir`、state-reconcile `--state-dir` 由 0 改為 2；shadow-verify `--mapping`、onboard `--rule-files`（什麼檔都沒選到、`''`、字面路徑不存在或是迴圈；同時給其他 phase 時由 0 改為 2）由 1 改為 2；state-reconcile 讀不出的既有 `--manifest-path` 由 0（覆寫）改為 2（`manifest_unreadable`，不覆寫；0 bytes 視同不存在）；generator-service `--config-subdir` 在啟動時的 HEAD 樹裡不是目錄時拒絕啟動（完全沒有則 stderr 說明後照常啟動）。沒給旗標時的預設路徑不判定。symlink 迴圈改判為 `*_not_accessible`，config-diff `--old-dir` 是迴圈時由 1 改為 2。
