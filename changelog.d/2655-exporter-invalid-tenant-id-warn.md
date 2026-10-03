---
section: Added
topic: exporter
issues: [2655]
created: 2026-10-03T07:44:10+00:00
---
- **exporter 對不合法的 tenant id 印 WARN，照常載入（[ADR-035](docs/adr/035-tenant-id-single-source.md) D4；[#2655](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2655)）**：threshold-exporter 每次提交新設定（啟動載入、目錄或單檔 reload）時，對每個不符合 tenant id 規則（DNS-1123 label）的租戶各印一行 `WARN: tenant id "<id>" breaks the tenant-id rule; ...`，並附上規則說明。該租戶不會被拒收，它的 metric 照常輸出；不新增 metric，也不新增告警。內容沒有變動的 tick 不會 reload，所以不會重複印。看到這行，就代表該租戶需要改名。
