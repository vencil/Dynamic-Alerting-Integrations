---
section: Added
topic: exporter
issues: [3353]
created: 2026-11-12T17:00:00+00:00
---
- **threshold-exporter chart 新增可選的設定驗證 CronJob，判沒經過 CI 就進了線上 ConfigMap 的樹（[#3353](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3353)）**：`configValidation.enabled: true` 時，CronJob 定期（預設每 15 分鐘）在 exporter 讀的那份 ConfigMap 上跑 CI 用的同一支 `da-tools validate-config`，同一批列、同樣的嚴重度；有 FAIL 時 Job 失敗，由平台規則 `CronJobLastRunFailed` 告警，細節在 Job 的 log。記憶體不另設數字，取 exporter 的 `resources.limits.memory` × `memoryFactor`（預設 4，實測 validate-config 峰值約為 exporter 載入後的 2.5～3.2 倍）。預設關閉；告警只看 `monitoring` namespace；tenant-api 的 `direct` 寫入只留在它的 pod 裡、不會進這份 ConfigMap，不在判定範圍。映像 `v2.9.0` 的 `validate-config` 還沒有 `yaml_quoting`、`unread_keys`、`guard_errors`、`json_schema` 這些列，要等下一版 tools 才判得完整。
