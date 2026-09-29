---
section: Added
topic: alertmanager-routing
issues: [2325]
created: 2026-09-28T14:52:43+00:00
---
- **da-guard 與 tenant-api 也執行 `require_critical_escalation`（ADR-007；[#2325](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2325)）**：判準與 generator 相同（parity matrix 的 `escalation` 欄釘住兩種語言）。da-guard：不合規報 `critical_escalation_missing`（error），仍收得到 critical 的非 PagerDuty 目的地報 `critical_escalation_leak`（warn），值不是布林報 `domain_policy_unusable`。tenant-api：不合規的 PUT 回 403 `POLICY_VIOLATION`（constraint `require_critical_escalation`），batch 裡不合規的那筆為 `status: error`；洩漏只附在成功回應的 `warnings`（PR 模式 batch 裡同一租戶有多筆收進 PR 時，只留最後一筆的判定）。值照 generator（PyYAML）解讀：未加引號的 `yes`／`on` 是 true、`no`／`off` 是 false，加引號的 `"yes"` 不是布林；顯式 `!!bool` tag 照 PyYAML 不分大小寫（`!!bool yEs` 是 true）。PyYAML 整份拒收的值（如 `!!bool y`、`!!null {}`、`{<<: 1}`、`[!!bool y]`）generator 會丟掉整份檔、da-guard 報 `domain_policy_unusable`；tenant-api 對拒收值一律只關掉該 domain 的此約束並記 warn，同檔其餘約束照常生效（冷啟與熱重載皆同）。讀得出的非布林（如 `!!int 5`）與 `!!null x`（None）也只讓此約束失效。已知限制：tenant-api 只在寫入 tenant 時判定，改 `_routing_profiles.yaml` 或 `_domain_policy.yaml` 本身不會重判已存在的 tenant。
