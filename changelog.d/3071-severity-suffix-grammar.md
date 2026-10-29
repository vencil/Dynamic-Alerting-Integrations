---
section: Changed
topic: exporter
issues: [3071]
created: 2026-10-17T07:52:48+00:00
---
- **門檻值的 severity 後綴只收 `warning`／`critical`（exporter；[#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071)）**：⚠️ 行為變更。`值:severity` 改由一個解析函式讀（門檻 row 與 custom alert 共用）：取最後一個 `:` 之後、不分大小寫；`70:high`、空的 `70:` 這類規則不選的後綴，以前照寫送出、告警被靜默且無任何警告，現在與讀不懂的值同一條路——base key 改送平台預設值與 `warning`（`value_unparsed`），維度與 `optional_overrides:` 宣告的 key 不送該 series（`value_unparsed_dropped`）。含這類值的樹：da-guard 由 exit 0 變 1，validate-config 的 `guard_errors` 由 PASS 變 FAIL（0 → 1）；`70:Critical` 改以 critical 送出；config-diff 兩邊都是讀不懂的值時（如 `70:high` → `75:high`，兩邊都送預設值）由 1 變 0。schema `thresholdScalar` 加上後綴 pattern。升級注意：同時寫了 `70:Critical` 與 `<key>_critical` 的租戶，以前大寫那列被靜默，現在兩列同 series、整個 `/metrics` 回 500——先用新版 da-guard 檢查（`metrics_not_gatherable`，exit 1）再升級 exporter。
