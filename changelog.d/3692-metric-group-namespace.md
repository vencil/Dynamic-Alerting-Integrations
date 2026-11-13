---
section: Changed
topic: rule-packs
issues: [3692]
created: 2026-11-12T23:37:13+00:00
---
- **severity dedup 的 `metric_group` 改為每個規則包各自的命名空間（rule-packs / migrate_rule / generate-routes；[#3692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3692)）**：⚠️ breaking。mariadb 與 mongodb 原本都用 `metric_group: liveness`，同時使用兩包的租戶會被一包的 critical 抑制另一包的 warning；改名為 `mariadb_liveness`、`mongodb_liveness`。沒有 dedup 作用的單成員群組（`jvm_performance`、`kafka_active_controllers`、`kafka_broker_count`、`rabbitmq_consumers`）拿掉標籤。`migrate_rule` 產生的群組名改為完整的門檻鍵名（原本只取最後一段，`mysql_replication_lag` 與 `redis_replication_lag` 都會變成 `lag`）。`check_metric_group_pairs.py` 新增三項檢查：值跨規則包唯一、`slo_` 保留給 custom alert、群組要同時有 critical 與 warning。租戶路由覆寫的 `metric_group` 不是任何出貨規則包的群組時，產生器給 WARN（不擋）。外部 Alertmanager 或路由覆寫裡寫 `metric_group="liveness"` 等舊值的地方要跟著改。
