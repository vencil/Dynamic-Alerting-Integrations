---
section: Removed
topic: rule-packs
issues: [3783]
created: 2026-11-13T07:06:15+00:00
---
- **⚠️ Breaking：Kafka rule pack 移除兩條讀不到資料的告警（[#3783](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3783)）**：刪除 `KafkaNoActiveController`、`KafkaHighRequestRate`、`KafkaHighRequestRateCritical` 與其 recording rule，以及閾值 key `kafka_active_controllers`、`kafka_request_rate`、`kafka_request_rate_critical`，不留 alias。原因：pack 宣告的 danielqsj/kafka_exporter 不輸出 `kafka_controller_active_controller_count` 與 `kafka_server_brokertopicmetrics_messagesin_total`；後者是 JMX 指標，名稱隨各部署的 jmx_exporter 設定而異。遷移：外部 Alertmanager 以這三個 alertname 為 matcher 的 route／inhibit 請刪除；租戶 conf.d 裡這三個 key 請刪掉——寫入端（Portal／Tenant API）與 CI 的 validate-config 會以 `unread … (no_row)` 擋下；operator 自行在 `_defaults.yaml` 供給過 base 的部署，validate-config 改報 `no_alert`（不擋，但已沒有告警比對它），base 那行也一併刪除。
