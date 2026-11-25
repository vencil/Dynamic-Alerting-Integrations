---
section: Fixed
topic: rule-packs
issues: [4033]
created: 2026-11-25T15:30:00+00:00
---
- **`PostgreSQLHighDeadlocks` 改看過去 5 分鐘的死鎖次數，短時間爆發不再漏掉（rule-packs；[#4033](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4033)）**：原本的條件是每秒速率 `tenant:pg_deadlocks:rate5m > 0.1` 加 `for: 5m`。短時間的爆發（例如 30 秒內 60 次）滑出 5 分鐘窗口後速率歸零，條件最多成立約 4 分 45 秒，等不到 `for: 5m`，告警不會觸發。現在改為 `tenant:pg_deadlocks:increase5m > 30`、`for: 1m`（與 `DB2HighDeadlockRate` 同一做法）。門檻數值不變：0.1 次／秒 × 300 秒 = 每 5 分鐘 30 次，仍是寫在規則裡的常數，沒有租戶 key。比較的是 Prometheus 外推後的 `increase()`，原始次數剛好 29–30 時落在邊界上。告警說明改為「過去 5 分鐘 N 次死鎖」。⚠️ recording rule `tenant:pg_deadlocks:rate5m` 已移除，由 `tenant:pg_deadlocks:increase5m`（次數，不是每秒值）取代；自己的儀表板或規則若讀舊名稱，請改讀新名稱，或直接寫 `sum by(tenant, da_cluster) (rate(pg_stat_database_deadlocks[5m]))`。
