---
section: Fixed
topic: rule-packs
issues: [4328]
created: 2026-11-28T15:20:00+08:00
---
- **JVM 執行緒數告警現在也讀 Micrometer 的指標（rule-packs；[#4328](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4328)）**：`JVMThreadPoolExhaustion`／`JVMThreadPoolExhaustionCritical` 原本只讀 client_java／jmx_exporter 的 `jvm_threads_current`，Micrometer（Spring Boot Actuator）吐的是 `jvm_threads_live_threads`，所以用 Micrometer 的租戶設了 `jvm_threads` 也永遠不會響。現在 `tenant:jvm_threads:current` 兩個名字都讀，取每個租戶、每個叢集裡執行緒最多的那個 JVM。需 Micrometer 1.1 以上：1.0.x（Spring Boot 2.0）吐的是 `jvm_threads_live`，不讀，仍然不會響。⚠️ **已經設了 `jvm_threads`（或 `jvm_threads_critical`）、又用 Micrometer 的租戶，升級後會開始收到先前一直沒響的告警**：升級前請對照 `max by(tenant) (jvm_threads_live_threads)` 確認閾值合乎現況。record 名稱、閾值 key、alertname、`metric_group` 都沒變；只用 client_java／jmx_exporter 的租戶讀數不變。一個 JVM 同時吐兩個名字時只算一次，不會相加。
