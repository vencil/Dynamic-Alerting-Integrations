---
section: Fixed
topic: rule-packs
issues: [3928]
created: 2026-11-15T05:44:17+00:00
---
- **⚠️ breaking：JVM heap 告警改成量整體 heap（rule pack；[#3928](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3928)）**：`tenant:jvm_memory_used:percent` 原本逐 series 相除，Micrometer（Spring Boot Actuator）每個 heap pool 一條 series，讀到的是單一 pool。現在先把各 pool 加總再相除，取每個租戶最滿的 JVM。對 Micrometer 部署的影響依 GC 而異：Parallel／Serial 原本長期讀到 90% 以上（Eden、Survivor 正常就接近滿），`JVMMemoryPressure` 會少響；G1 原本只讀到 Old Gen，現在把 Eden、Survivor 的用量算進來，讀數高幾個百分點、會較早響；分代 ZGC 與分代 Shenandoah 原本偏低，現在照實讀。靠舊行為抓「Old Gen 滿、Eden 空」的部署會失去那個訊號：整體 heap 此時約 70%。已知限制（[#3976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3976)）：Parallel GC 預設旗標下讀數隨填充率越偏越低，到 OutOfMemoryError 都到不了 95，`JVMMemoryPressureCritical` 不會響；OpenJ9 balanced 讀成約四分之一（只讀了原始碼），告警不會響。同一條規則也讀 jmx_exporter 1.0 之前的 `jvm_memory_bytes_used`／`jvm_memory_bytes_max`，這類部署設了 `jvm_memory` 之後會開始收到 `JVMMemoryPressure` 與 `JVMMemoryPressureCritical`。閾值與預設值沒有動，`jvm_memory` 仍由 operator 供給。
