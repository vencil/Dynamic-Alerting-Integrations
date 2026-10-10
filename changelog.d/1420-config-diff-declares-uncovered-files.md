---
section: Fixed
topic: da-tools
issues: [1420]
created: 2026-10-10T11:42:34+08:00
---
- **`config-diff` 不再對它沒比對的檔案說「沒有變更」（[#1420](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1420)）**：只改 `_defaults.yaml`（每個沒覆寫的租戶都繼承）、`_platform.yaml` 等 `_` 開頭的檔，或子目錄裡的檔時，以前印 `No changes detected.` 並回 0。現在報告開頭的「Changed Files Not Compared」區段列出這些兩側內容不同的檔名，結束碼為 1，JSON 輸出多一個 `uncovered_files`。報告仍不列出這類變更影響的租戶，這些檔要人工審。`da-tools init` 與 CI/CD 精靈產出的 workflow 註解同步更新。
