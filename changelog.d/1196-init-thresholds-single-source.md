---
section: Fixed
topic: onboarding
issues: [1196]
created: 2026-09-27T00:29:24+00:00
---
- **`da-tools init` 產出的閾值改用與 `scaffold` 同一份來源（tools；[#1196](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1196)）**：init 原本自帶一份 `defaults:`，67 個 key 裡 36 個沒有任何告警在讀（例：`jvm_heap_usage`、`redis_memory_usage`、nginx／clickhouse／db2 全部），設了也不會觸發。現在 `_defaults.yaml` 與租戶檔的 critical 種子都取自 `rule-packs/threshold-registry.yaml`，部分預設值隨之改變（例：`kafka_consumer_lag` 10000→1000、`pg_connections_critical` 150→90）。`check_threshold_reachability` 新增一面：產出任何沒有告警讀的 key 即失敗。⚠️ 已產生的 `_defaults.yaml` 不會自動改；重新產生後，租戶檔裡的舊 key 會被判為 unknown key（tenant-api 寫入會被擋），請依 issue 1196 的對照表改成新 key（例：`jvm_heap_usage`→`jvm_memory`）。
