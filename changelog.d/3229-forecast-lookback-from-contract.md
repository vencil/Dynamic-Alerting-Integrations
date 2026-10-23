---
section: Changed
topic: custom-alerts
issues: [3229]
created: 2026-10-23T13:00:00+00:00
---
- **forecast 的 lookback 由 recipe 契約決定，`max_range_duration: 96h` 改由產生器寫入（custom-alerts；[#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229)）**：`recipe_contract.json` 的 `forecast` 新增 `lookback_factor`（2）與 `lookback_floor`（`1h`），編譯器的 lookback 讀這兩個值，不再寫死。compiled pack 的豁免上限（最大 horizon 的 lookback，今天是 96h）由 `gen_recipe_contract.py` 寫進 `.github/custom-rule-policy.yaml`、`docs/custom-rule-governance{,.en}.md` 與 `_lib_constants.FORECAST_MAX_LOOKBACK`（`lint_custom_rules` 的內建 policy 讀它）；改契約沒重產時 `recipe-contract-check` 擋下。tenant-api 拒收數字型 `quantile`／`objective` 的訊息不再提已刪除的 Python 編譯器。`compile_custom_alerts` 遇到解不開或讀不到的檔案一律回 2、點名那條路徑、不寫出 pack：這種樹本來就在編譯前被共用的 conf.d 拒收擋下；若檔案在拒收檢查之後、編譯之前才壞掉，原本只印一行 ⚠ 並以 ✅、rc 0 寫出少了那些租戶的 pack，現在同樣拒收。
