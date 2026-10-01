---
section: Fixed
topic: exporter
issues: [2419]
created: 2026-09-30T23:38:08+00:00
---
- **da-guard 不再把「與根 `_defaults.yaml` 維度鍵同值」的租戶維度覆寫判為多餘（da-guard；[#2419](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2419)）**：根 `_defaults.yaml` 寫 `pg_connections{env="prod"}: 50`、租戶也寫同值時，過去 `redundant_override` 建議刪掉租戶那一行，但刪掉之後 `/metrics` 少了 `env="prod"` 那條 series——根層的維度鍵只送成一條不帶該 label 的獨立 series，不會替租戶送出帶 label 的 series。現在只有根 `_defaults.yaml` 寫的維度鍵不再算繼承值；子目錄 `_defaults.yaml`、根平台檔 `tenants:` 與 profile 寫的維度鍵照舊，同值時仍判為多餘。根 `_defaults.yaml` 的 `*_critical` 鍵是另一個問題（#2544），本次未處理。
