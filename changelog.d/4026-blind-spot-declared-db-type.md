---
section: Changed
topic: tools
issues: [4026]
created: 2026-11-18T23:20:00+00:00
---
- **⚠️ BREAKING：`blind-spot` 只看租戶宣告的 `_metadata.db_type`，判決不再隨執行時刻改變（tools；[#4026](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4026)）**：先前以「這一刻 `/metrics` 送了哪些閾值鍵」配上鍵名前綴推 DB 類型。這有三個問題：內建層的鍵每個租戶都有列，樹裡只要有一個租戶，mariadb／postgresql 等類永遠不會是盲點；排程停用的時段跑會被判成盲點；`/metrics` 收集不了的時段跑會 rc 2。現在照 exporter 的讀法經 `da-guard served-values --config-facts` 讀宣告，也就是 `tenant_expected_exporter` 用的那一份。沒宣告 `db_type` 的租戶，送了該類的鍵也不算有監控。
