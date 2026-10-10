---
section: Fixed
topic: alertmanager-routing
issues: [2817]
created: 2026-10-10T19:30:00+08:00
---
- **平台告警 `labels:` 裡的樣板值不再被當成字面值比對（`generate-routes`；[#2817](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2817)）**：`TenantMetricsOverLimit` 的 `tenant: "{{ $labels.tenant }}"` 原本以樣板字串本身當探針值，對 `tenant` 下正規式的 inhibit target 一律對不上，完整探針集反而比內建 fallback 寬鬆：由租戶告警觸發、會壓掉該告警的 inhibit，在 `--output-configmap`／`--apply` 的平台不變式檢查中被放行。現在 label 值裡的 `{{ … }}` 片段一律比照 expr `by (tenant)` 推得的 tenant，換成同一個 placeholder 再比對（前後的字面部分保留）；營運方 base config／叢集設定裡這類 inhibit 現在會被擋下。generator 自己產的 inhibit 不受影響。
