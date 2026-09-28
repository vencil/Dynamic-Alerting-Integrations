---
section: Added
topic: platform
issues: [1360]
created: 2026-09-28T06:24:29+00:00
---
- **`CronJobLastRunFailed` 與 `MassExporterOutage` 有了處置章節（[#1360](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1360)）**：Troubleshooting Checklist 新增 §1.7「平台自我監控告警」，兩條告警的 `runbook_url` 指向各自章節。至此每條平台告警都帶 `runbook_url`，閘門不再保留豁免清單：新增的平台告警沒有 `runbook_url` 就會被擋下。
