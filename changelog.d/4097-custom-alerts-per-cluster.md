---
section: Changed
topic: custom-alerts
issues: [4097]
created: 2026-11-23T03:30:00+00:00
---
- **自訂告警（`Custom_*`）逐叢集判定（exporter／rule packs；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：同一個租戶跑在好幾個叢集時，編譯出的自訂告警原本把各叢集合在一起算：`max` 只留較大的叢集、`sum` 把叢集相加、比值混用各叢集的分子分母、SLO 的長短窗可以來自不同叢集，告警也不說是哪個叢集。現在每個 recipe 都保留 `da_cluster`（`by(tenant, version, da_cluster)`；SLO 以 `and on(tenant, da_cluster)` 配對），每個越界的叢集各發一則、帶 `da_cluster` 與該叢集自己的值；閾值與維護狀態仍是租戶一份。⚠️ 行為改變（只在序列帶 `da_cluster` 的 Prometheus 上）：`rate`、`ratio`、`forecast` 比值模式、`slo_burn_rate` 不再跨叢集合算，要兩個叢集相加才越界的告警不再觸發，被另一個叢集流量沖淡的比值會觸發；告警多一個 label，Alertmanager 視為新的一條。沒有 `da_cluster` 的單一 Prometheus 結果不變。`absence` recipe 維持「任一個叢集有就算有」，編譯出的規則上方以 `# da_cluster: tenant-wide` 註明（`da-guard compile-custom-alerts` 的 JSON 多一個 `declarations` 欄位）。recipe 的 `selectors` 不再接受 `da_cluster`（載入時拒收）。`cluster_identity_baseline.yaml` 清單歸零。
