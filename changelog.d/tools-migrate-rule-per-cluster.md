---
section: Fixed
topic: da-tools
issues: [1200]
created: 2026-11-24T23:30:00+00:00
---
- **`migrate` 產生的規則逐叢集聚合，`validate` 以（租戶、叢集）對值（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：recording rule 的外層分組由 `<sum|max> by(tenant)` 改為 `by(tenant, da_cluster)`（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）；閾值與維護狀態仍是租戶一份。⚠️ **行為變更**：在收了多個叢集資料的 Prometheus 上，遷移後的規則逐叢集評估（先前兩個叢集各 60、閾值 100 會以合計 120 告警，現在不告警），告警帶 `da_cluster`，`{{ $labels.da_cluster }}` 照留；`old_query` 同樣分組。原式子自己的聚合已丟掉 `tenant` 的規則不在此列。原式子自己寫的 `on(...)`／`by(...)` 照抄：點名 `tenant`／`instance`／`pod`／`namespace`／`node` 卻沒有 `da_cluster` 的會在報告與規則檔標出來請人確認（該不該補取決於配對兩邊是否都帶它）；`--no-ast` 時說「未檢查」。`validate` 逐叢集比對，只在一邊出現的叢集列為 `mismatch`，`Tenant` 欄寫成 `<租戶> (da_cluster=<叢集>)`。序列沒有 `da_cluster` 時結果不變。舊版已產生的規則維持 `by(tenant)`：在已部署的 `platform-recording-rules.yaml` 找 `tenant:<key>:sum`／`:max` 的 `by(tenant) (`，以同一份舊規則重跑 `da-tools migrate` 後換上新的規則檔與 `prefix-mapping.yaml`；工具沒有專門找它們的指令。
