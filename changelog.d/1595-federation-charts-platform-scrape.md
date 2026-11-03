---
section: Changed
topic: platform-self-monitoring
issues: [1595]
created: 2026-11-03T18:40:00+00:00
---
- **federation-gateway／federation-reconciler 改依平台 scrape label 被抓，不必再裝進 `monitoring`（helm；[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595) D1b）⚠️ BREAKING**：兩個 chart 的 Service 帶上 `dynamic-alerting.io/scrape: platform`，raw 模式改由 `platform-components` 在任何 namespace 逐 pod 抓取；`allow-prometheus-egress` 加開 3903／9099。gateway 的 NetworkPolicy 先前只放行 listen port，metrics port 的 scrape 一律被擋，現依新值 `networkPolicy.scrapeNamespaceLabel` 放行（預設 `monitoring`；`null` = 任何 namespace，兩個 chart 相同）。⚠️ 原本裝在 `monitoring`、由 `monitoring-components`（role: service）抓的安裝：series 改為逐 pod，多出 `pod` label、`instance` 變成 pod IP、counter 重新起算，未聚合的 `FederationRevocation*`／`FederationGateway*` 告警 label 集合隨之改變，以它們寫的 route、silence、inhibit 要改。
