---
section: Changed
topic: platform-self-monitoring
issues: [1595]
created: 2026-11-04T00:36:00+00:00
---
- **⚠️ BREAKING：`helm/vector` 的 `metrics.enabled` 改為預設 on，加上 ingress NetworkPolicy，租戶投影丟棄告警重做（helm／rule-packs；[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595)）**：之前預設 off，`VectorBufferEventsDropped` 與 `TenantProjectionFanoutDiscardSpike` 在預設安裝下恆不開火；chart 0.11.0 起預設開啟（`helm upgrade --reuse-values` 會沿用舊值，請明確設 `metrics.enabled: true`）。新增預設開啟的 ingress `NetworkPolicy`：scrape port 只放行 `networkPolicy.scrapeNamespaceLabel`（預設 `monitoring`，`null` 為任何 namespace），Prometheus 不在該 namespace 的部署要改這個值。新增 `tenant_eligible` filter，非 audit 列不再進 `tenant_project`；新告警 `TenantProjectionMappingErrors` 抓投影 VRL 的 runtime 錯誤；`TenantProjectionFanoutDiscardSpike` 拿掉 `> 5/s`，改成高於自身 1 日平均 3 倍且 `> 0.1/s`，只抓突增、不是 registry 不同步偵測器。
