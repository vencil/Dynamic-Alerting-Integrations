---
section: Added
topic: alertmanager-routing
issues: []
created: 2026-09-26T17:00:00+00:00
---
- **Inhibit 語意防護、合成探測路由與 silence 命中報告（alertmanager、tools）**：Alertmanager 視 `equal:` 中「兩側皆缺」的 label 為相等，會靜默吞掉不相干的通知；`generate_alertmanager_routes.py` 現在於組裝時檢查每條 inhibit rule 的 `equal:` label 是否兩側都有 presence gate（`--strict` 下失敗、否則 WARN），BYO Alertmanager 檢查新增以 live 告警佐證的 `alertmanager_inhibit_semantics`。新增保留 route：帶 `component="synthetic-probe"` 的告警一律導向專屬 receiver，可用自有探測器端到端驗證投遞鏈而不驚動 on-call。`silencer-drift-check` 回報每條 silence 實際命中的規則（`coverage[]`），可發現過寬的 matcher。domain policy（ADR-007）違規在 `--strict` 下由 WARN 升為 blocking，非 strict 行為不變。
