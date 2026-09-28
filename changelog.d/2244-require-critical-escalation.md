---
section: Added
topic: alertmanager-routing
issues: [2244]
created: 2026-09-28T08:00:44+00:00
---
- **domain policy 的 `require_critical_escalation: true` 開始生效（ADR-007；[#2244](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2244)）**：先前只有 lint 認得這個鍵，`generate_alertmanager_routes.py` 不檢查。現在要求 tenant 至少有一條 `severity=critical` 的路徑通往 PagerDuty：主 receiver 的 type 是 `pagerduty`，或有一條會產出的 `routes` 條目 `match` 含 `severity: critical` 且送往 `pagerduty` receiver。不合規時與其他 constraint 一樣，`--strict` 為 ERROR（exit 1）、預設為 WARN；值不是布林時 `--strict` 也報 ERROR。合規但仍有 critical 告警到不了 PagerDuty 的兩種情況只報 WARN、不影響 exit code：排在升級路由之前、可能攔下 critical 的非 PagerDuty override 或 route；以及升級路由除 `severity` 外還比對別的 label，其餘 critical 告警落到非 PagerDuty 的 receiver。
