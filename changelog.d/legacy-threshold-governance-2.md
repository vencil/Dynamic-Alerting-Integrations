---
section: Added
topic: threshold-governance
issues: [656, 916]
created: 2026-09-26T17:00:00+00:00
---
- **閾值 `expires:` 時限化覆寫與下界（`<`）閾值推薦（threshold-exporter、threshold-recommend）**：結構化閾值新增 `expires:`（RFC3339）與 `reason:`，事故中臨時調鬆的覆寫到期後自動回落平台 default，並發 `da_config_event{event="threshold_expired"}`；v1 只適用 `_defaults.yaml` 內有 default 的標準指標，malformed 時保留覆寫並警告。下界閾值改分三態：`percentile-lower`（`db2_bufferpool_hit_ratio`）走 P5 floor 推薦、放鬆 floor 一律交人工；`not-applicable` 不推薦；其餘維持 `needs_review`。`--generate-observed-map` 改為 merge-preserve，regen 不再輾平人工解析的條目；這兩項預設零行為變化。見 [#916](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/916)、[#656](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/656)。
