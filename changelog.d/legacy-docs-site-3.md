---
section: Added
topic: docs-site
issues: [1599]
created: 2026-09-26T17:00:00+00:00
---
- **告警最佳實務系列文章與 Grafana dashboard 導覽（docs）**：新增雙語三篇系列文章——〈告警設計入門〉談該對什麼告警（`docs/alerting-design-fundamentals.md`）、〈多嚴才算嚴〉談 SLO／錯誤預算／burn-rate 告警（`docs/alerting-slo-error-budget.md`）、〈Actionable 之後〉談告警動作的冪等與防線（`docs/alerting-best-practices.md`），互鏈成完整成熟度階梯並接進首頁、架構 Hub、遷移與入門指南。Grafana dashboard 導覽由四份補成六份（新增 Federation Audit 與 Tenant Log Query），並說明出廠只自動佈署 MariaDB Overview 與 Federation Revocation Reconciler，其餘由 operator 依需要匯入。ADR-034 昇格 accepted。
