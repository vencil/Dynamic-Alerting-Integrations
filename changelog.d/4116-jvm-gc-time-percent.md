---
section: Changed
topic: rule-packs
issues: [4116, 4032]
created: 2026-11-28T02:00:00+00:00
---
- **⚠️ breaking：JVM GC 告警改看最忙的 JVM 的時間佔比，閾值 key 換成 `jvm_gc_time_percent`（rule-packs；[#4116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4116)）**：`JVMHighGCPause`／`JVMHighGCPauseCritical`（alertname 不變）原本比租戶各 JVM 每秒 GC 暫停秒數的加總，現在比 `tenant:jvm_gc_time:percent`：每個租戶、每個叢集最忙的那個 JVM 花在 GC 暫停的時間百分比。Micrometer（`jvm_gc_pause_seconds_sum`）與 client_java／jmx_exporter（`jvm_gc_collection_seconds_sum`，不計 ZGC／Shenandoah 的 `Cycles`）都讀。舊 key `jvm_gc_pause`／`jvm_gc_pause_critical` 退役、沒有 alias：還寫著的設定在每一層都報 error 並點名新 key。換算：舊值是各 JVM 加總的秒／秒（registry 曾誤標 `seconds/5m`，[#4032](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4032)），新值是單一 JVM 的百分比，請依自己的 baseline 重訂，不要直接乘以 100；參考值 10／25，平台仍不出貨值。`metric_group` 由 `jvm_gc_pause` 改為 `jvm_gc_time_percent`：routing override 還寫舊 group 只會得到一則 WARN，之後不路由任何告警。record `tenant:jvm_gc_pause:rate5m` 已移除。Micrometer 早於 1.7.0（Shenandoah）、1.8.0（ZGC）、1.12.4（分代 ZGC；1.11.x 沒有）會把整個 concurrent cycle 記成暫停，讀數偏高。內建層摘要由 `968ca3d1c003` 變為 `981b5923a866`。
