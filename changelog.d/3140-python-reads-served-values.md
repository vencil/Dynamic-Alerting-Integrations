---
section: Changed
topic: exporter
issues: [3140]
created: 2026-10-21T16:30:00+00:00
---
- **backtest 與 config-diff 只讀 served-values 的拒收與停用欄位（[#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)）**：門檻值讀不讀得懂、是不是停用、被拒收後送什麼，只由 exporter（da-guard）判定，Python 不再另寫一份（ADR-036）。⚠️ 行為變更。`backtest --tenant` 的每個值改由 `da-guard served-values` 讀（工具建一棵以出廠平台預設值為根、只有這個 key 的樹），讀它送出的值、severity 與 `keys[].reason`／`refused`，所以 `--tenant` 現在需要 da-guard（找不到時結束碼 2）。新的一邊因值被拒收為非有限數而不送 row（`_critical`、`optional_overrides:` 宣告的 key 寫 `NaN`／`Inf`）時判 HIGH、結束碼由 0 回到 1；目錄比對以 served-values 的 `value_non_finite`（`--schedules` 時段或 `keys[].reason`）同樣判 HIGH。被拒為讀不懂的值（`value_unparsed`，含不加引號的 `.nan`／`.inf`）與 `disable` 維持 MEDIUM，`1_000`、`0x1p4` 照舊不回測，結束碼都不變。config-diff 的 `toggled` 與 `disabled` 顯示改讀 `keys[].reason`（JSON 該筆另帶 `disabled` 側別清單），不再出列的 custom alert 的 note 改讀 `custom_alerts[].status`／`refused`，分得出刪除、停用、門檻被拒收時寫明。`_lib_validation.is_disabled`（與 `_lib_python` 的轉出）已刪除；`custom_alert_disabled` 隨 blast-radius 退役一併刪除。 unit-sanity 照 exporter 讀 `1_000` 的底線，十六進位仍不判。
