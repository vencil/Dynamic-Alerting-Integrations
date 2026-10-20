---
section: Changed
topic: exporter
issues: [3140]
created: 2026-10-20T16:58:00+00:00
---
- **門檻值必須是有限數（exporter、da-guard、validate-config、tenant-api、backtest；[#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)）**：⚠️ 行為變更。`NaN`、`Inf`、`-Inf`、`infinity` 以前照寫送出（告警永不觸發或永遠觸發），現在與讀不懂的值同一條路：base key 改送平台預設值，`_critical`、宣告、維度鍵與子目錄 `_defaults.yaml` 的值不送；da-guard 與 validate-config 由 0 變 1，tenant-api 寫入與 dry-run 由 200 變 400。根 `_defaults.yaml` 寫 `.nan`／`.inf` 的 key 不送預設列、比照 `optional_overrides:`，da-guard 報 `root_default_non_finite`，由 0 變 1。config-diff「與預設相同 → 非有限數」由 1 變 0；backtest `--tenant` 的 base key 改成非有限值時以平台預設值計算，由 1 變 0；子目錄寫 `"NaN:critical"` 時 config-diff 與 backtest `--config-dir` 由 2 變 0。⚠️ 暫時狀態：`_critical`、宣告 key 寫非有限值時 backtest 判成「Metric disabled」MEDIUM、由 1 變 0，改判 HIGH 由後續 PR 依 served-values 的拒收原因實作。unit-sanity 遇 ±Inf 照舊報 OUT-OF-DOMAIN；migration-audit 不再建議 `.inf`／`.nan`。
