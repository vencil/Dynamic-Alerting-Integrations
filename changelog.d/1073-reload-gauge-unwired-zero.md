---
section: Fixed
topic: tenant-api
issues: [1073]
created: 2026-11-21T23:28:09+00:00
---
- **沒接上 reload observer 的設定元件不再永遠顯示「重載成功」（tenant-api；[#1073](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1073)）**：`tenant_api_config_last_reload_successful` 的起始值由 1 改為 0（尚未確認）。啟動時接上 observer 的 manager 會被回放成初次載入的真實結果，在伺服器開始服務前就讀到 1 或 0；漏接的 component 則停在 0，`TenantApiConfigReloadStale` 會觸發，不再是永遠的假綠。該告警的說明補上「0 也可能代表該 component 沒接上 observer」；新增測試保證實際接線的 component 集合與指標的 component 清單一致。
