---
section: Changed
topic: threshold-governance
issues: [3990]
created: 2026-11-15T23:45:30+00:00
---
- **⚠️ BREAKING：`threshold-recommend` 以 exporter 一整天實際送出的值比較，不再在執行當下讀 `/metrics`（da-tools；[#3990](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3990)）**：租戶自己寫的 key 改以 `served-values --schedules-only` 的整天送出值計算建議，不再以檔案文字計算；載入警告改取自它本來就讀的 `da-guard effective`。所以結果不再隨執行時刻改變，`/metrics` 收集不了的樹也照常執行，改由主閘門擋。寫成排程、帶 `expires:`、一天中送出不只一個值或等級、等級不是該 key 層級、或整天都沒送出的 key，改為 `force_manual`，原因寫在 `guardrail_reason`；JSON 每筆另有 `served_value`（比較時用的值）。
