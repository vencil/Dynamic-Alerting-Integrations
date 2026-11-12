---
section: Fixed
topic: log-aggregation
issues: [1295]
created: 2026-11-12T17:00:00+08:00
---
- **monitoring namespace 的 NetworkPolicy 不再擋掉 log store 的讀寫與 scrape（k8s、victorialogs、chargeback-aggregator；[#1295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1295)）**：`k8s/03-monitoring` 對整個 namespace default-deny，原本 Grafana 只放行 Prometheus，VictoriaLogs datasource 與從 grafana.com 安裝外掛（443）都被擋；Prometheus 與 VictoriaLogs 兩端都沒放行 9428，store 與 vmauth 從未被 scrape；chargeback-aggregator 沒有自己的 NetworkPolicy，連 DNS 都出不去。現在三者都放行，chargeback-aggregator chart（0.4.0）新增只放行 DNS 與 VictoriaLogs 的 egress policy，victorialogs chart（0.5.1）的 ingress 多放行 `app: prometheus`。裝在 VictoriaLogs 以外 namespace 的 consumer（federation-gateway／reconciler）仍要在 victorialogs 的 `networkPolicy.allowedNamespaces` 加上它的 namespace。
