---
section: Changed
topic: rule-packs
issues: [3309, 1200]
created: 2026-11-06T00:52:41+00:00
---
- **⚠️ BREAKING：維護期間要不要擋，改由每條租戶告警的 `maintenance: mute｜page` label 決定（rule-packs、maintenance-scheduler、自訂告警編譯器；[#3309](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3309)）**：label 是唯一寫下決定的地方，PromQL 的維護條件由 `scripts/tools/dx/generate_rulepack_derived.py` 依 label 寫進 expr 尾端，pre-commit 以 `--check` 擋手改；kubernetes pack 的 `:core` recording rule 不再帶維護條件，改由告警帶。行為變更：`recurring:` 週期維護的 silence 只擋 `maintenance="mute"` 的告警，叢集存活、exporter 缺席與單實例 critical 停機在週期窗內也會照常通知，與一次性 `_state_maintenance` 一致；`PostgreSQLRecentRestart`、`MariaDBRecentRestart`、`ClickHouseHighFailedQueryRate` 改為維護期間不觸發。所有租戶告警（含編譯的自訂告警）多了 `maintenance` label，以告警 label 做路由或比對的設定要一併檢查。[告警參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/ALERT-REFERENCE.md)新增「維護期間」欄。
