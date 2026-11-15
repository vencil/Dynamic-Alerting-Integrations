---
section: Added
topic: rule-packs
issues: [3973]
created: 2026-11-15T23:20:07+00:00
---
- **RabbitMQ 的 memory／disk alarm 告警（`RabbitMQMemoryAlarm`、`RabbitMQDiskAlarm`；[#3973](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3973)）**：任一節點的 memory 或 free-disk alarm 生效時，RabbitMQ 會擋住所有 publisher，原本 pack 裡沒有任何規則會響；`vm_memory_high_watermark = 0`（上限為 0、立刻擋住 publisher）時，比值告警 `RabbitMQHighMemory` 也因為濾掉上限為 0 的節點而靜默。新增兩條狀態型告警（critical、`for: 1m`、維護模式靜音、不讀閾值），兩種 exporter 都讀：kbudde/rabbitmq_exporter 的 `rabbitmq_node_mem_alarm`／`rabbitmq_node_disk_free_alarm`，內建 rabbitmq_prometheus plugin 的 `rabbitmq_alarms_memory_used_watermark`／`rabbitmq_alarms_free_disk_space_watermark`。新增 recording rule `tenant:rabbitmq_memory_alarm:max`、`tenant:rabbitmq_disk_alarm:max`。`RabbitMQMemoryAlarm` 與 `RabbitMQHighMemory` 同屬 `metric_group: rabbitmq_node_mem_percent`：它響時壓住那條 warning，租戶以這個 metric_group 寫的路由覆寫也會一併帶走它。plugin 只報本節點，每個節點都要抓，`/metrics/detailed` 不帶這兩個指標。
