---
section: Added
topic: rule-packs
issues: [3737, 3771]
created: 2026-11-12T15:52:42+00:00
---
- **新告警 `TenantExporterReplicaDown`：同租戶仍有其他 target 為 up 時，單一 tenant-exporters target 掛掉也會告警（rule-packs；[#3737](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3737)）**：liveness pack 新增逐 target 規則，`up{job="tenant-exporters"} == 0` 且同租戶有任一 target `up == 1` 滿 10 分鐘即響，`severity: warning`、`maintenance: mute`。只對在 `_metadata` 宣告 `db_type` 的租戶生效（[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)），與 `TenantExporterAbsent` 的納管範圍一致。租戶所有 target 都掛時它不響，互斥寫在 expr 裡，由 `TenantExporterAbsent`（critical）接手。同一個 db_type 有兩個 Service、其中一個全掛且它的 target 仍在服務探索裡（`up == 0`）時，這條 warning 是唯一的訊號；若掛掉的 target 從服務探索消失（沒有 `up` 序列），這條與 `TenantExporterAbsent` 都不會響，是已知缺口。**升級須知**：宣告了 `db_type`、有多個 target 的租戶，升級後若有一直 `up == 0` 的 target（長期壞掉的副本、scrape 設錯多出來的 port 或 Service），會開始持續收到這條 warning；升級前可先查 `up{job="tenant-exporters"} == 0`。
