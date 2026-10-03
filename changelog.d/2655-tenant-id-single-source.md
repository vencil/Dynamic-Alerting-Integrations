---
section: Changed
topic: confd-family
issues: [2655]
created: 2026-10-03T07:04:11+00:00
---
- **⚠️ breaking：tenant id 一律須為 DNS-1123 label，不合法時產生器在所有模式失敗（[ADR-035](docs/adr/035-tenant-id-single-source.md)；[#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655)）**：規則只寫在 `tenant-config.schema.json` 的 `definitions.tenantId`（1–63 個小寫英數與 `-`，首尾為英數），產生器、da-guard、tenant-api、schema 檢查、portal、`scaffold`／`onboard`（不合法時回 2、不寫檔）與 operator 工具都讀它；Go 與 portal 讀產生的副本，`tenant-id-json-check` 擋漂移。含大寫、`_` 或超過 63 字元的 id 不再合法：`generate-routes` 在 render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、`--strict` 一律回 1、不寫出也不套用設定（線上設定維持原狀）；CI 的 schema 與 da-guard 檢查轉紅；tenant-api 對該 id 的所有寫入回 400，讀取不變。請把租戶改名為 DNS-1123 形式（全數字 id 要加引號），silence、inhibit 與 metric 的 `tenant` label 隨之改變。exporter 不受影響、照常載入（後續 PR 加 WARN）。
