---
section: Fixed
topic: federation
issues: [1235, 1236, 1238, 1288, 1295, 1313]
created: 2026-09-26T17:00:00+00:00
---
- **Federation 撤銷在實際部署下失效的洞（helm、gateway、reconciler）**：文件化拓撲下 store ConfigMap 與 gateway／reconciler 不在同一 namespace，撤銷從未生效——新增 `federation.store.namespace`（預設不變），跨 namespace 安裝**必須**設定。`revoked.txt` 缺失新增 critical 告警 `FederationGatewayRevokedSetMissing`；gateway 與 reconciler 對 `revoked.txt` 改採同一契約（`revokedSet.tokenIdPattern`），⚠️ gateway 遇到違反契約的行即作廢整輪 reload、沿用前一份撤銷集，並另增兩條 critical 告警；撤銷告警改為鎖存 1 小時（reconciler 另加 `reconcile.projectionGraceSeconds`）。⚠️ 行為變更：Vector 分流改綁 producer namespace（`evidenceChannel.podNamespace`／`.gatewayNamespace`），非預設 namespace 的 producer 須設定。見 [#1313](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1313)、[#1288](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1288)。
