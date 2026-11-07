---
section: Changed
topic: da-tools
issues: [3446, 3693]
created: 2026-11-03T16:04:12+00:00
---
- **⚠️ Breaking：輸入目錄存在卻無法列出內容時改為呼叫端錯誤，不再當成空目錄（ops 工具；[#3446](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3446)）**：state-reconcile `--state-dir`（明確給的或預設的）無法列出時由 0（以 0 筆 state 覆寫 manifest）改為 2、不寫檔；migrate-to-operator `--source-dir` 被 da-guard 標為 `unlistable_dir` 時由 0（只印 WARNING、以 0 個來源檔產出遷移）改為 2。config-diff `--old-dir` 無法列出時由 1（當成第一次匯入、所有租戶都算新增、stderr 全空）改為 2；`--new-dir` 無法列出或沒有設定檔時仍是 2，但改為一行點名、不輸出報告（先前都報成 `has no config file`）；grafana-import `--dashboard-dir` 無法列出或沒有 `*.json` 時仍是 2，改為點名旗標與路徑（先前只印 `No dashboard files found.`）（[#3693](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3693)）。兩者帶 `--json` 時 stdout 皆為空、原因只在 stderr。內部：輸入旗標改以 `_lib_input.add_input` 宣告，Python 工具的目錄列舉收斂為一個原語。
