---
section: Added
topic: alertmanager-routing
issues: [2325]
created: 2026-09-28T14:52:43+00:00
---
- **da-guard 與 tenant-api 也執行 `require_critical_escalation`（ADR-007；[#2325](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2325)）**：判準與 generator 相同（`tests/shared/routing_policy_parity_matrix.json` 的 `escalation` 欄釘住兩種語言）。da-guard：不合規報 `critical_escalation_missing`（error），仍收得到 critical 的非 PagerDuty 目的地報 `critical_escalation_leak`（warn），值不是布林報 `domain_policy_unusable`。tenant-api：不合規的 PUT／batch 回 403 `POLICY_VIOLATION`（constraint `require_critical_escalation`），洩漏只附在成功回應的 `warnings`。已知限制：tenant-api 只在寫入 tenant 時判定，改 `_routing_profiles.yaml` 或 `_domain_policy.yaml` 本身不會重判已存在的 tenant；未加引號的 `yes` 在 generator 算 true、在 Go 端是非布林。
