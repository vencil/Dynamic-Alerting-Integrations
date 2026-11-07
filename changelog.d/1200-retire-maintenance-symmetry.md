---
section: Removed
topic: rule-packs
issues: [1200, 3309]
created: 2026-11-07T00:08:16+00:00
---
- **移除 pre-commit hook `maintenance-symmetry-check`（`check_maintenance_symmetry.py`；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：它是 maintenance 子句的第二套拼法規則，還允許一條告警帶 2 個子句，與 `generate_rulepack_derived.py --check`（[#3309](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3309)）衝突。子句的數量與拼法改由後者單獨把關：`mute` 恰好一個標準子句（連同它讀的 recording rule 一起算）、`page` 零個，非標準拼法一律拒絕；`tenant_metadata_info` 的用法由 `check_leftouterjoin_enrichment.py` 把關。
