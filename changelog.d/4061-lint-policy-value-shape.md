---
section: Changed
topic: tools
issues: [4061]
created: 2026-11-18T23:50:12+00:00
---
- **`lint --policy` 頂層的值在載入時判定，型別不符即結束碼 `2`；時長改照 Prometheus 的語法讀（tools；[#4061](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4061)）**：⚠️ breaking。`denied_functions`／`denied_patterns`／`required_labels` 須為非空字串的清單（可為空清單）；`max_range_duration`／`max_evaluation_interval` 須為正的 Prometheus 時長。先前 `max_range_duration: banana`（或 `0s`、`2w`）讓範圍檢查整個失效、`denied_functions` 寫成單一字串時逐字元比對而不禁止那個函式、`null` 或整數則崩潰。時長（政策值與 group 的 `interval`）改照 Prometheus 的 `ParseDuration` 讀：`1h30m`、`2w` 可用，`1.5h`、不加引號的 `5` 被拒。訊息點名鍵與原因（原因碼 `policy_invalid`）。
