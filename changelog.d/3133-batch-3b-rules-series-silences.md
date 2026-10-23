---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-23T09:00:00+00:00
---
- **⚠️ 結束碼變更：runtime-audit、discover-mappings、cutover、maintenance-scheduler 讀 rules／series／label values／silences 沒得到回答時結束碼 2（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133) 第三批）**：lib 新增 `prometheus_rules`／`rules_groups`、`prometheus_series`、`prometheus_label_values`、`silences_list`，回應格式由它們檢查。runtime-audit 遇 `data: null`、缺 `groups` 或 group／rule 型別不對，先前把每條規則報成 MISSING（0／1 → 2，`--runtime-json` 同樣檢查）；discover-mappings 查詢失敗先前當成沒有標籤（1 → 2）；cutover 的健康檢查回應格式不對先前當成沒有閾值指標（1 → 2）或通過（`NaN` 計數，0 → 2）；maintenance-scheduler 有窗口要建 silence 卻讀不到既有的，先前照常建立、可能重複（0 → 2，不建立任何 silence；沒有窗口時不讀）。
