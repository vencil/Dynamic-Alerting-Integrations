---
section: Added
topic: exporter
issues: [2452]
created: 2026-09-30T23:55:00+00:00
---
- **conf.d 掃描失敗有了指標與告警（exporter；[#2452](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2452)）**：執行中的 exporter 若出現同一租戶 id 被兩個檔宣告，每次掃描都失敗、整棵樹停在最後一版正確設定，任何租戶的修改都不生效（有無 `_defaults.yaml` 都會）；先前只有每 tick 的 log，`da_config_reload_trigger_total` 不產生序列，`ConfigReloadStuck` 也因此不會觸發。新增 `da_config_scan_failures_total{reason}`（封閉集合 `duplicate_tenant`／`walk_error`，兩者從 0 起；watch 路徑每次掃描失敗加 1，不動 reload 計數）與平台告警 `ConfigScanFailing`（critical）：設定已超過 5 分鐘無法掃描——有掃描失敗，且 `da_config_last_scan_complete_unixtime_seconds` 超過 5 分鐘未更新——時觸發，reload 間隔 30s 到 420s 實測都在 10 分鐘內，修好後第一個成功的 tick 就解除。`/ready` 行為不變，仍回 200。
