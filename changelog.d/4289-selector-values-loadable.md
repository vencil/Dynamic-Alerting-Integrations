---
section: Fixed
topic: custom-alerts
issues: [4289]
created: 2026-11-24T23:08:03+00:00
---
- **自訂告警的 selector 值改為同步確認 Prometheus 載入得了（exporter／tenant-api；[#4289](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4289)）**：`selectors_re` 編不過的 regex（例如 `(unclosed`），以及任何 selector 值裡的 U+FFFD（tenant-api 收到的 JSON 中無效 UTF-8 位元組解碼後即是它），以前會通過寫入與編譯、產生整包都無法載入的規則，讓 Prometheus 停在上一份規則。現在與其他無效欄位一致：tenant-api 回 400 並列出 violation、exporter 不送該條、`da-guard compile-custom-alerts` 略過並警告；放行範圍由測試對 Prometheus 解析器逐一比對。⚠️ 既有設定裡若有這類值，該條宣告會從「產生壞規則」變成「被拒」。
