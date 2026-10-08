---
section: Changed
topic: confd-family
issues: [2751]
created: 2026-10-08T19:00:00+00:00
---
- **`maintenance-scheduler` 的排程改讀 exporter 的答案（[#2751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2751)）**：⚠️ 行為變更：每個租戶的 `_state_maintenance.recurring` 改取 `da-guard effective`，不再由 Python 重讀 YAML。根目錄平台檔 `tenants:` 裡寫的、子目錄租戶檔裡寫的排程，原本讀不到、不建 silence 也沒有訊號，現在與根目錄租戶檔一樣建立；`_defaults.yaml` 頂層的 `_state_maintenance` 仍不算（與 exporter 相同）。找不到 da-guard、exporter 讀不到某個檔、或 da-guard 失敗（含 `--config-dir` 裡沒有任何 `.yaml`）時改為 exit 2，不再當成「沒有排程」。缺 cron／duration 的項目照舊 WARN 跳過，`reason` 缺省仍為 `Recurring maintenance`。
