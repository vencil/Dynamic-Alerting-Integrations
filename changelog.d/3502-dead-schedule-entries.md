---
section: Changed
topic: exporter
issues: [3502]
created: 2026-11-07T04:07:30+00:00
---
- **排程裡永遠不生效的條目改為回報並擋下（exporter、da-guard、tenant-api；[#3502](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3502)）**：被寫在前面的 window 完全遮住的 window、被 window 合起來蓋滿整天的 `default`，一天中沒有任何一分鐘會送出，以前靜默。現在 `da-guard effective`／`served-values`／`key-verdict` 逐條目帶 `dead_entries`，共用判決報 `dead_entry`（error），掛在寫下它的檔（含 profile）；⚠️ **breaking**：含死條目的樹會讓 validate-config、`generate-routes --validate` 轉紅，tenant-api 與 `da-guard write` 拒收新寫或改過的死條目（檔裡原封不動的只回 NOTICE）。部分重疊仍合法。`da_config_values_not_served` 的說明改由原因表產生（補上漏列的 `spelling_duplicate`），Python 讀取端遇到未知的 `not_served` 原因改為拒收。
