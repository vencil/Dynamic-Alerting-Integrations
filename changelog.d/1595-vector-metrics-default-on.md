---
section: Changed
topic: platform-self-monitoring
issues: [1595]
created: 2026-11-04T00:36:00+00:00
---
- **⚠️ BREAKING：`helm/vector` 的 `metrics.enabled` 改為預設 on，租戶投影丟棄告警重做（helm／rule-packs；[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595)）**：之前預設 off，`VectorBufferEventsDropped` 與 `TenantProjectionFanoutDiscardSpike` 在預設安裝下恆不開火。chart 0.11.0 起預設渲染 `vector-metrics` Service 與 9598 port；不要的話設 `metrics.enabled: false`，代價是這組告警失效。新增 `tenant_eligible` filter，非 `federation_audit` 的列不再進 `tenant_project`；新告警 `TenantProjectionMappingErrors` 抓投影 VRL 的 runtime 錯誤（`intentional="false"`）。`TenantProjectionFanoutDiscardSpike` 拿掉 `> 5/s` 常數，改成未投影 audit 列高於自身 1 日平均 3 倍且 `> 0.1/s`；它只抓突增，不是 registry 不同步偵測器。
