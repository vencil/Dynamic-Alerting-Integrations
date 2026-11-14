---
section: Fixed
topic: log-aggregation
issues: [1295]
created: 2026-11-12T17:00:00+08:00
---
- **monitoring namespace 的 NetworkPolicy 不再擋掉 log store 的讀寫與 scrape（k8s、victorialogs、chargeback-aggregator；[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）**：`k8s/03-monitoring` 對整個 namespace default-deny，原本 Grafana 只放行 Prometheus，VictoriaLogs datasource 與從 grafana.com 安裝外掛（443）都被擋；Prometheus 與 VictoriaLogs 兩端都沒放行 9428，store 從未被 scrape（vmauth 自己的 metrics 在內部埠，仍不在 scrape 範圍）；chargeback-aggregator 沒有自己的 NetworkPolicy，連 DNS 都出不去。現在三者都放行，chargeback-aggregator chart（0.4.0）新增只放行 DNS 與 VictoriaLogs 的 egress policy，victorialogs chart 的 ingress 多放行 `app: prometheus`。⚠️ chargeback-aggregator 升級後的行為改變：policy 以 `app.kubernetes.io/name: victorialogs`、9428 和 `networkPolicy.victorialogsNamespaceLabel`（預設 monitoring）選 store；store 在別的 namespace 要改那個 label，不是用 helm/victorialogs 裝的或不在 9428 要設 `networkPolicy.enabled: false`。裝在 VictoriaLogs 以外 namespace 的 consumer，要在 victorialogs 該 consumer 的 `auth.consumers[].from.namespace` 寫上它的 namespace。
