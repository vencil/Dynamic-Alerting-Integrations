---
section: Fixed
topic: custom-alerts
issues: [1200]
created: 2026-11-06T17:01:55+00:00
---
- **自訂告警的 selector 不能再釘 `le`（recipe contract；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：p99_latency recipe 以 `sum by(le, …)` 聚合 histogram bucket，selector 若釘住 `le`，`histogram_quantile` 就少了 `+Inf` bucket、算出 NaN，告警從此不觸發也沒有提示。`le` 已列入 `reserved_labels`，這樣的設定會在載入時被拒；threshold／rate recipe 原本可以用釘 `le` 的方式過濾 bucket（例如只看快請求），這種寫法也一併被擋，請改用不含 `le` 的指標。新增的測試從編譯結果讀出每個 recipe 聚合租戶指標所用的 label，確認都在保留清單裡，之後新 recipe 漏列會直接失敗。
