---
section: Fixed
topic: confd-family
issues: [2119]
created: 2026-10-08T16:31:27+00:00
---
- **`backtest --config-dir/--baseline` 改比 exporter 實際送出的值（[#2119](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2119)）**：⚠️ 行為變更：兩棵樹都由 `da-guard served-values` 讀，逐租戶、逐門檻 key 比 `/metrics` 的值，不再由 Python 重讀 YAML、比租戶檔頂層 key。只改平台檔、`_defaults.yaml`、profile 或子目錄不再回 0 個變更；`tenants:` 包裝格式不再回報成 metric=`tenants`；沒有 `tenants:` 的平鋪檔 exporter 不送，改它不再算變更。新增／移除的租戶與關掉的 key 那一邊記為無值；只在一天其他時段不同的排程值帶 `window`。找不到 da-guard、da-guard 失敗，或任一棵樹有 exporter 讀不了的檔時 exit 2（帶 `--skip-if-unavailable` 也一樣），不給部分答案。
