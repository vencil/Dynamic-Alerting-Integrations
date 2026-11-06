---
section: Fixed
topic: federation
issues: [2550]
created: 2026-11-05T23:59:17+00:00
---
- **`FederationGatewayRevocationLoadFailure` 不再說「已撤銷的 token 正被放行」（rule-packs、Grafana、runbook；[#2550](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2550)）**：gateway 讀不到撤銷清單時，會沿用上一次成功載入的撤銷集，其中的 token 仍被擋；受影響的是那次載入之後發出的撤銷，以及首次載入就失敗、沒有前一份可用的 worker（該 worker 不執行任何撤銷）。告警的 summary／description、federation 撤銷 dashboard 的三個 panel 描述、[Grafana dashboard 說明](docs/grafana-dashboards.md)、runbook、ADR-028 的攻擊者視角段落，以及 try-local 的 chaos 說明，都改成這個說法。告警名稱、觸發條件與 dashboard 的 panel 標題都沒有改。
