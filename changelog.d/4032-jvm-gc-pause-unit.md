---
section: Fixed
topic: rule-packs
issues: [4032]
created: 2026-11-20T20:40:00+00:00
---
- **⚠️ `jvm_gc_pause` 的單位標示改為實際的「秒／秒」（rule-packs；[#4032](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4032)）**：`jvm_gc_pause` 與 `jvm_gc_pause_critical` 原本標示 `seconds/5m`，但規則比的是 `rate(jvm_gc_pause_seconds_sum[5m])`，也就是每秒累積的 GC 暫停秒數（租戶各 JVM 加總）；0.5 代表每秒有半秒在暫停，不是 5 分鐘暫停 0.5 秒。規則、key 與值都沒有變，只改了 registry、pack 註解、portal 顯示的單位，以及 `JVMHighGCPause`／`JVMHighGCPauseCritical` 告警 `description` 的文字。照舊標示（秒／5 分鐘）填過值的部署，設定值比原意大 300 倍，告警不會響，請重新檢視。
