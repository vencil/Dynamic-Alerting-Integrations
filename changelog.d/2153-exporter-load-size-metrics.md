---
section: Added
topic: exporter
issues: [2153]
created: 2026-09-28T02:23:34+00:00
---
- **新增載入規模與冷載入耗時指標（exporter；[#2153](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2153)）**：`da_config_max_tenants_per_file`（單一檔案 `tenants:` 宣告的租戶數最大值）與 `da_config_max_mapping_keys`（單一 mapping 鍵數最大值）兩個 gauge 取整棵樹的最大值、不帶檔名 label，每次設定 commit 重設；這兩個數字越大，載入與 reload 的時間成長得比線性更快。`da_config_initial_load_duration_seconds` 記錄啟動時那一次載入的秒數，既有的 `da_config_reload_duration_seconds` 語意不變。`da_config_reload_duration_seconds` 的 bucket 追加 `60, 120, 300, 600`、`da_config_scan_duration_seconds` 追加 `10, 30, 60, 120`：既有 `le` series 的值不變，只多出新的 series；原本超過舊上界、落在 `+Inf` 的分位數查詢，現在會算出實際值。
