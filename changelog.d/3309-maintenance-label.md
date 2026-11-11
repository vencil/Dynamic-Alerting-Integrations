---
section: Changed
topic: rule-packs
issues: [3309, 1200]
created: 2026-11-06T00:52:41+00:00
---
- **⚠️ BREAKING：維護期間要不要擋，改由每條租戶告警的必填 label `maintenance: mute｜page` 決定（[#3309](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3309)）**：維護條件由 `generate_rulepack_derived.py` 依 label 寫入 expr，pre-commit 擋手改；編譯的自訂告警與 `da-tools migrate` 的產物也帶 label。維護開啟時（含 `recurring:` 週期窗，見 [#3639](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3639)）只擋 `mute`，叢集存活、exporter 缺席與單實例 critical 停機在週期窗內會通知；`*RecentRestart`、`ClickHouseHighFailedQueryRate` 改為維護時不觸發。升級注意：告警多一個 label，身分改變，部署時觸發中的告警會 resolve 再觸發、`for:` 重算；自寫規則的 label 要與 expr 是否帶維護條件一致（帶 `unless … user_state_filter{filter="maintenance"}` 就是 `mute`）。[告警參考](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/ALERT-REFERENCE.md)新增「維護期間」欄。
