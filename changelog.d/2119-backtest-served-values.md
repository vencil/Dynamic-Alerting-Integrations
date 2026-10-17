---
section: Fixed
topic: confd-family
issues: [2119, 2750, 3060, 3111]
created: 2026-10-08T16:31:27+00:00
---
- **`backtest --config-dir/--baseline` 改比 exporter 實際送出的值（[#2119](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2119)）**：⚠️ 行為變更：兩棵樹都由 `da-guard served-values` 讀，逐租戶、逐條 `/metrics` series 比值（[#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)），不再由 Python 重讀 YAML。只改平台檔、`_defaults.yaml`、profile 或子目錄不再回 0 個變更；`tenants:` 包裝格式不再回報成 metric=`tenants`；沒有 `tenants:` 的平鋪檔 exporter 不送，改它不再算變更。新增／移除的租戶與不再送出的 series 那一邊記為無值；排程值每組新舊值一列，`window` 列出成立的時段。由哪支告警比取自 series（[#2750](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2750)），舊拼法別名的 twin 不查。severity 是 series 的 label：只改 severity 是兩列，`70:high` 讀不懂、改送預設值（[#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071)），`--tenant` 同一規則（[#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)）。已知限制：帶維度的 key 照列但不回測（`not_backtested`）。找不到 da-guard、da-guard 失敗，或任一棵樹有 exporter 讀不了的檔時 exit 2（帶 `--skip-if-unavailable` 也一樣），不給部分答案。
