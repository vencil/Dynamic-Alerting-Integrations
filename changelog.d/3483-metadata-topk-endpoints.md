---
section: Changed
topic: rule-packs
issues: [3483]
created: 2026-11-01T14:45:33+00:00
---
- **⚠️ BREAKING：平台 Prometheus 改為逐 pod 抓取平台元件，副本掛掉看得到（k8s；[#3483](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3483)）**：`platform-components` 從 `role: service` 改為 `role: endpointslice`（Prometheus 需要 `discovery.k8s.io` endpointslices 的讀取權限），每個 pod 抓一次並加上 `pod` label；平台 Service 的 `prometheus.io/port` 必須是 pod 監聽的 port（tenant-api chart 已改）。過去 `up{job="threshold-exporter"}` 看不到掛掉的副本、`ThresholdExporterTooFewReplicas` 恆觸發。每個 exporter 序列現在各有一份 per 副本：內建規則、面板與 da-tools 的計數一律先 `max without(instance, pod)`（直接告警的租戶規則、Grafana、byo/federation/cutover/shadow 檢查），當 join 右側的改成每個 join key 一筆（kubernetes 的 crashloop／imagepull opt-in、自訂告警編譯器的 threshold 紀錄）。自己寫的規則或面板照同樣方式收斂。
