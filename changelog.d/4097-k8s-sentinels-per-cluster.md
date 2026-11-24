---
section: Changed
topic: rule-packs
issues: [4097]
created: 2026-11-22T22:25:00+00:00
---
- **`CustomRecipeDiskInert` 與 `VersionAwareThresholdInert` 逐叢集判定（rule packs；[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）**：同一個租戶跑在好幾個叢集時，這兩條「設定了卻沒有對應資料」的告警原本只要有一個叢集有資料就不響。現在在收了各叢集資料的 Prometheus 上逐叢集判定，不需要宣告：`CustomRecipeDiskInert` 對「租戶有 Running pod、卻沒有 volume-stats」的每個叢集各發一則；`VersionAwareThresholdInert` 對「設了版本閾值的租戶有 Pod、卻完全沒有 Pod label 資料」的每個叢集各發一則，只跑其他租戶的叢集不響。兩條在那裡多帶 `da_cluster`（Alertmanager 視為新的一條），annotation 寫出叢集。⚠️ 行為改變（沒有 `da_cluster` 的單一 Prometheus 也適用）：設了版本閾值的租戶全都沒有 Pod、別的租戶有 Pod、又沒有 Pod label 資料時，`VersionAwareThresholdInert` 以前會響，現在不響，該租戶的第一個 Pod 出現後才響；其餘情況不變。⚠️ 限制：同一個叢集的兩種資料要帶同一個 `da_cluster`；只有一種帶時，兩條告警會對正常的叢集誤報（見 ADR-006）。`CustomRecipeDiskInert` 的 `$value` 改為該叢集的 Running pod 數（annotation 不使用）。
