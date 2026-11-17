---
section: Changed
topic: rule-packs
issues: [985]
created: 2026-11-16T16:25:31+00:00
---
- **`TenantHAReplicasDegraded` 寫出是哪一類 workload 在降級（rule-packs；[#985](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/985)）**：告警多一個 annotation `degraded_workload_kinds`，列出降級中的類別（`statefulset`、`deployment`，同時降級時兩個都列）。過去兩臂在合併前就只剩 `tenant`，看不出是哪一類。recording rule `rule_pack_kubernetes:ha_replicas_degraded:core` 改名為 `:by_kind` 並帶 `workload_kind`，⚠️ 讀舊名的外部查詢要改成 `max by(tenant) (rule_pack_kubernetes:ha_replicas_degraded:by_kind)`。告警仍是每租戶一條，label、summary 與 `for: 15m` 計時不變，降級在兩類之間轉移時持續 firing。annotation 在 Alertmanager UI／API 上隨每次評估更新；已送出的通知是當時的值，要等下次重送才會更新。
