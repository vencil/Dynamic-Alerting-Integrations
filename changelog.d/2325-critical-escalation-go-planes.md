---
section: Added
topic: alertmanager-routing
issues: [2325]
created: 2026-09-28T14:52:43+00:00
---
- **da-guard 也執行 `require_critical_escalation`（ADR-007；[#2325](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2325)）**：判準與 generator 相同（parity matrix 的 `escalation` 欄釘住兩種語言）。da-guard：不合規報 `critical_escalation_missing`（error），仍收得到 critical 的非 PagerDuty 目的地報 `critical_escalation_leak`（warn），值不是布林報 `domain_policy_unusable`。值照 generator（PyYAML）解讀：未加引號的 `yes`／`on` 是 true、`no`／`off` 是 false，加引號的 `"yes"` 不是布林；顯式 `!!bool` tag 照 PyYAML 不分大小寫（`!!bool yEs` 是 true）。PyYAML 整份拒收的值（如 `!!bool y`、`!!null {}`、`{<<: 1}`、`[!!bool y]`）generator 與 da-guard 都整份拒收（[#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486)）。讀得出的非布林（如 `!!int 5`）與 `!!null x`（None）只讓此約束失效。
