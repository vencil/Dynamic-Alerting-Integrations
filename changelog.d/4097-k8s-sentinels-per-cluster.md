---
section: Changed
topic: rule-packs
issues: [4097]
created: 2026-11-22T22:25:00+00:00
---
- **`CustomRecipeDiskInert` 與 `VersionAwareThresholdInert` 逐叢集判定（rule packs；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：同一個租戶跑在好幾個叢集時，這兩條「設定了卻沒有對應資料」的告警原本只要有一個叢集有資料就不響。現在在收了各叢集資料的 Prometheus 上逐叢集判定，不需要另外宣告：`CustomRecipeDiskInert` 對「租戶有 Running pod、卻沒有 volume-stats」的每個叢集各發一則；`VersionAwareThresholdInert` 對「有租戶 Pod、kube-state-metrics 卻完全沒有 Pod label 資料」的每個叢集各發一則（前提仍是有任何租戶設了版本專屬閾值）。兩條告警在那裡多帶 `da_cluster` label，annotation 寫出叢集。序列沒有 `da_cluster` 的單一 Prometheus 上，觸發條件、label 與 annotation 文字都不變。⚠️ 設了 `da_cluster` 的部署：這兩條告警多一個 label，Alertmanager 會把它當成新的一條；`CustomRecipeDiskInert` 的 `$value` 由閾值改為該叢集的 Running pod 數（annotation 不使用它）。
