---
section: Fixed
topic: da-tools
issues: [1420]
created: 2026-10-10T11:42:34+08:00
---
- **`config-diff` 不再對改過的平台檔說「沒有變更」（[#1420](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1420)）**：只改 `_defaults.yaml`、`_platform.yaml` 等 `_` 開頭的檔時，以前印 `No changes detected.` 並回 0。現在報告的「Changed Platform Files」區段列出兩側內容不同的 `_` 開頭檔（任何深度，只改註解也算），結束碼為 1，JSON 輸出的 `uncovered_files` 是同一份清單。這類變更影響的租戶由渲染比較報出；兩支渲染程式都讀不到的設定（例如平台層的 `_custom_alerts`）只能看這份清單人工審。`da-tools init` 與 CI/CD 精靈產出的 workflow 註解同步更新。
