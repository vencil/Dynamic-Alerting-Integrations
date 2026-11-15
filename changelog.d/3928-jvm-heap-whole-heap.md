---
section: Fixed
topic: rule-packs
issues: [3928]
created: 2026-11-15T05:44:17+00:00
---
- **⚠️ breaking：JVM heap 告警改成量整體 heap（rule pack；[#3928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3928)）**：`tenant:jvm_memory_used:percent` 原本逐 series 相除。Micrometer（Spring Boot Actuator）每個 heap pool 一條 series，讀到的是最滿的那個 pool，而 Eden 這類 pool 正常運作就接近滿：整體 heap 只用 18% 時 `JVMMemoryPressure` 也會響。現在先把各 pool 加總再相除，取每個租戶最滿的一個 JVM；沒有自己上限的 pool（G1 的 Eden、Survivor 回報 -1）只計入用量。同一條規則也讀 jmx_exporter 1.0 之前的指標名 `jvm_memory_bytes_used`／`jvm_memory_bytes_max`，這類部署原本沒有樣本、也就沒有這條告警。`JVMMemoryPressureCritical` 與 `JVMPerformanceDegraded` 讀同一條序列，一併改變。對使用者的影響：Micrometer 的部署，這三條告警會少響（原本的多半是誤報）；舊版 jmx_exporter 的部署在設了 `jvm_memory` 之後會開始收到。閾值與預設值都沒有動，`jvm_memory` 仍由 operator 供給。
