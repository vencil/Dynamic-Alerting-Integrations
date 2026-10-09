---
section: Fixed
topic: exporter
issues: [2031]
created: 2026-10-09T08:30:00+00:00
---
- **根 `_defaults.yaml` 的維度鍵改送成帶 label 的 series（threshold-exporter / da-guard；[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）**：根 `_defaults.yaml` 寫 `pg_connections{env="prod"}: 30` 時，過去 `/metrics` 送成一條 `metric` label 是整串鍵、不帶 `env` label 的獨立 series；現在與子目錄 `_defaults.yaml` 相同，對沒寫這個鍵的租戶送成帶 `env="prod"` 的 series，租戶自己寫了同一鍵（任一拼法）時只送租戶的值，租戶值的 `expires:` 到期後退回根層的值。`/effective`、`da-guard served-values` 與 `/metrics` 一致。⚠️ 行為變更：查詢舊形狀 `metric="connections{env=\"prod\"}"` 的告警或面板要改用 label。根 `_defaults.yaml` 的維度鍵現在算繼承值，與其同值的租戶覆寫判為 `redundant_override`（[#2419](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2419) 的暫時處置不再適用）。
