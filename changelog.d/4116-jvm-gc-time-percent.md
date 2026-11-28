---
section: Changed
topic: rule-packs
issues: [4116, 4032]
created: 2026-11-28T02:00:00+00:00
---
- **⚠️ breaking：JVM GC 告警改看最忙的 JVM 的時間佔比，閾值 key 換成 `jvm_gc_time_percent`（rule-packs；[#4116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4116)）**：`JVMHighGCPause`／`JVMHighGCPauseCritical`（alertname 不變）原本比租戶各 JVM 每秒 GC 暫停秒數的加總，現在比 `tenant:jvm_gc_time:percent`：每個租戶、每個叢集最忙的 JVM 花在 GC 暫停的時間百分比；Micrometer 與 client_java／jmx_exporter（`jvm_gc_collection_seconds_sum`，不計 `Cycles`）都讀。舊 key `jvm_gc_pause`／`_critical` 退役、沒有 alias：還留著的設定每一層都報 error，換成新 key 之前**沒有 GC 告警**。新值請依自己的 baseline 重訂（舊值是加總的秒／秒，[#4032](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4032)），不要乘以 100；參考值 10／25，平台不出貨值。告警只帶 tenant 與 da_cluster，哪一台要自己查：`topk(3, 100 * sum without(action, cause, gc) (rate(jvm_gc_pause_seconds_sum{tenant="<租戶>"}[5m])))`。`metric_group` 改為 `jvm_gc_time_percent`：routing override 寫舊 group 只得一則 WARN、不再路由。record `tenant:jvm_gc_pause:rate5m` 已移除。Micrometer 早於 1.7.0（Shenandoah）、1.8.0（ZGC）、1.12.4（分代 ZGC）把 concurrent cycle 記成暫停，讀數偏高。內建層摘要 `968ca3d1c003`→`981b5923a866`。
