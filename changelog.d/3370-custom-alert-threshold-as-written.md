---
section: Changed
topic: exporter
issues: [3370]
created: 2026-10-31T02:22:59+00:00
---
- **custom alert 的 `threshold:` 改依原文讀取，與租戶閾值鍵同一個讀法（exporter；[#3370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3370)）**：先前 `_custom_alerts` 清單經 YAML 依型別讀過一次再轉手，裸寫的 `0x1F` 照送 31、`0o17` 與 `017` 照送 15。現在與 [#3250](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3250) 相同，以檔案寫下的原文經 `parseThresholdNumber` 讀：`017` 讀成 17，`0x1F`、`0o17`、`0b101`、`5_` 拒收。**Breaking**：這幾種寫法的 alert 以前會送出，現在被拒收；da-guard 閘門對它們由 rc 0 改為 rc 1。同一個讀法也用在 `/effective`、merged_hash 與 tenant-api 的 custom-alerts 讀寫：portal 顯示的是 exporter 實際送出的值，存檔不再把 `017` 改寫成 15。加引號或一般十進位寫法不受影響。
