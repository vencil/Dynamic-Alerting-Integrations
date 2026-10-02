---
section: Fixed
topic: exporter
issues: [2592]
created: 2026-10-02T16:40:00+00:00
---
- **conf.d 讀不到或 `_defaults.yaml` 壞掉時，告警訊號與實際狀態對齊（exporter；[#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592)）**：設定目錄列不出內容、或樹裡已沒有可用的設定檔時，整棵樹本來就停在上一份設定，但先前掃描仍算成功，`ConfigScanFailing` 不會響，階層模式還每個 tick 把所有租戶計成 `reload_trigger{delete}`；現在計為 `da_config_scan_failures_total{reason="root_unreadable"|"empty_tree"}`、不更新 last-scan gauge、不排 reload。新增兩個狀態型 gauge 與平台告警：`da_config_unreadable_files{reason}`（讀不到而被略過的檔／子目錄數；`ConfigFilesUnreadable`，warning）與 `da_config_defaults_unusable{reason}`（解析失敗或讀不到的 `_defaults.yaml` 數；`ConfigDefaultsUnusable`，critical），兩條都是 `> 0` 持續 10 分鐘，檔案壞多久就響多久，修好後解除。
