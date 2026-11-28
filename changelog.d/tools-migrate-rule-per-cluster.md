---
section: Fixed
topic: da-tools
issues: [1200]
created: 2026-11-24T23:30:00+00:00
---
- **`validate` 以（租戶、叢集）對值（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：同一個租戶在兩個叢集的兩列不再互相覆蓋（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）。`validate` 逐叢集比對，只在一邊出現的叢集列為 `mismatch`，`Tenant` 欄寫成 `<租戶> (da_cluster=<叢集>)`；`shadow-verify` 一個租戶不論幾個叢集只算一次。序列沒有 `da_cluster` 時結果不變。
