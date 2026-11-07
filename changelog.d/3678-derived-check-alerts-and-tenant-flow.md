---
section: Fixed
topic: rule-packs
issues: [3678]
created: 2026-11-07T11:11:33+00:00
---
- **維護條件生成器補上兩個缺口（rule-packs；[#3678](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3678)）**：`generate_rulepack_derived.py --check` 新增兩項拒收。第一，凡是可能選到 `ALERTS`／`ALERTS_FOR_STATE` 的 selector 一律拒收：`page` 告警經由 mute 告警的 ALERTS 會在維護窗內被一起吃掉。第二，維護條件左側丟掉 `tenant` 的拒收：`sum without(tenant) (…) unless on(tenant) (…)` 永遠配不上，維護期間照樣觸發。左側是否保有 `tenant` 由新的 `da-read promql-label-flow` 在 Prometheus 解析樹上判定，會一路追進它讀的 recording rule；判不出來的一律拒收。兩種寫法的實際後果由 `tests/rulepacks/maintenance-derived-gaps_test.yaml` 的 promtool 案例證明。現有 16 個 pack 沒有命中。
