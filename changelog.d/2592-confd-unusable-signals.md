---
section: Fixed
topic: exporter
issues: [2592]
created: 2026-10-02T16:40:00+00:00
---
- **conf.d 讀不到或 `_defaults.yaml` 壞掉時，告警訊號與實際狀態對齊（exporter；[#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592)）**：設定目錄列不出內容、或已沒有可用的設定檔時，樹本來就停在上一份設定，但先前掃描仍算成功、`ConfigScanFailing` 不響，階層模式還每 tick 把所有租戶計成 `reload_trigger{delete}`；現在計為 `da_config_scan_failures_total{reason="root_unreadable"|"empty_tree"}`，不更新 last-scan gauge、不排 reload。冷啟動沒有上一份可保留，根目錄只列出一部分時仍載入讀得到的部分。新增狀態型 gauge `da_config_unreadable_files{reason}`（告警 `ConfigFilesUnreadable`，warning）與 `da_config_defaults_unusable{reason}`（解析失敗或讀不到的 `_defaults.yaml` 數；`ConfigDefaultsUnusable`，critical），皆 `> 0` 持續 10 分鐘，問題持續多久就響多久。
