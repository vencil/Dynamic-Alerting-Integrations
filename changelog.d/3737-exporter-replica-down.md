---
section: Added
topic: rule-packs
issues: [3737]
created: 2026-11-12T15:52:42+00:00
---
- **新告警 `TenantExporterReplicaDown`：同租戶仍有其他 target 為 up 時，單一 tenant-exporters target 掛掉也會告警（rule-packs；[#3737](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3737)）**：liveness pack 新增逐 target 規則，`up{job="tenant-exporters"} == 0` 且同租戶有任一 target `up == 1` 滿 10 分鐘即響，`severity: warning`、`maintenance: mute`。租戶所有 target 都掛時它不響，互斥寫在 expr 裡，不依賴 Alertmanager inhibit；此時由 `TenantExporterAbsent`（critical）接手只限在 `_metadata` 宣告 `db_type` 的租戶，沒宣告的租戶全掛時沒有任何告警。同租戶的健康 target 可能屬於別的 Service，這時掛掉那個 Service 的監控其實已全斷（缺口見 [#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）。**升級須知**：同租戶有多個 target 的部署，升級後若有一直 `up == 0` 的 target（長期壞掉的副本、scrape 設錯多出來的 port 或 Service），會開始持續收到這條 warning；升級前可先查 `up{job="tenant-exporters"} == 0`。
