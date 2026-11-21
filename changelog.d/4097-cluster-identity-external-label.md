---
section: Changed
topic: tools
issues: [4097]
created: 2026-11-21T17:00:00+00:00
---
- **⚠️ breaking：每台 Prometheus 要設 `external_labels.da_cluster`（[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)，ADR-006 修訂）**：叢集身分改由 Prometheus 的 external label 提供，一台一行，抓取 job 不必寫任何東西。Prometheus 會把 external label 加在送往 Alertmanager 的告警、`/federate` 與 remote write 上，所以告警與中央 Prometheus 都分得出叢集；只評估自己叢集的 Prometheus，序列上沒有這個 label，規則照舊逐租戶計算。ADR-006 原本寫的「每個抓取 job 用 relabel 寫入」要寫四處、只寫一部分時容器告警會靜默，已改掉。**`da-tools federation-check edge` 多一項檢查 `edge_cluster_identity`**：實際那台 Prometheus 的 `external_labels.da_cluster` 沒有值就失敗（原本會過的邊緣叢集現在會被指出來）；它也不再把設定裡別處的 `cluster` 當成 external label。本 repo 自己的設定由 `check_scrape_reachability` 的 `CLUSTER-IDENTITY` 檢查同一件事。**升級動作**：在 `prometheus.yml` 的 `global.external_labels` 加 `da_cluster: <叢集 ID>`（prometheus-operator 寫在 Prometheus CR 的 `spec.externalLabels`）；單一叢集也要設，HA 對用同一個值。設上之後 Alertmanager 收到的每條告警都多一個 `da_cluster` label，新舊告警會並存到舊的過期。**限制**：一台 Prometheus 直接抓好幾個叢集時要自己在抓取 job 上寫；邊緣叢集本地查詢看不到這個 label。
