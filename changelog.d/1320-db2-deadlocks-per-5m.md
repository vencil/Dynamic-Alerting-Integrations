---
section: Changed
topic: rule-packs
issues: [1320, 1176]
created: 2026-11-20T17:10:00+00:00
---
- **⚠️ breaking：DB2 deadlock 閾值改成「每 5 分鐘的次數」，舊 key `db2_deadlock_rate` 退役（rule-packs；[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：`DB2HighDeadlockRate` 原本拿每秒速率比閾值，建議值 5 等於每 5 分鐘 1500 次，真實的 deadlock storm 到不了，告警實際上不會響。新 key `db2_deadlocks_per_5m` 比的是過去 5 分鐘的 deadlock 次數（`increase`，租戶的各 exporter 加總），`for` 由 5m 縮為 1m。仍需自行設定，建議起點 5（Grafana ibm-db2 mixin 的閾值；它逐資料庫判斷、`for` 5m）；參考庫量到一個正常水位約每 5 分鐘 6 次的資料庫會誤警，請依自己的 baseline 取值。遷移：把 `db2_deadlock_rate` 改名為 `db2_deadlocks_per_5m` 並重新取值（換算是乘 300，但舊值多半本來就到不了）；舊 key 留在設定裡會被判為 error 並點名新 key。自己維護 `_defaults.yaml` 或覆寫過 Helm `optional_overrides` 清單的部署，清單裡的 `db2_deadlock_rate` 也要換成新 key，否則租戶填了新 key 也不會生效。alertname 不變；告警的 `description` 改為次數；recording rule 改為 `tenant:db2_deadlocks:increase5m`。
