---
section: Changed
topic: tools
issues: [3671]
created: 2026-11-07T12:56:16+00:00
---
- **⚠️ BREAKING：da-assembler 每個 CR 寫成自己的目錄，`spec.defaults` 終於有人讀；`spec.stateFilters`、`spec.profile` 改為拒收（tools、k8s CRD；[#3671](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3671)）**：先前 CR 渲染成 `<name>.yaml`，`spec.defaults`、`spec.stateFilters` 寫在這個租戶檔的頂層，exporter 一律不讀（只印 `should only be in _defaults.yaml, ignoring`）。現在租戶寫到 `<name>/<name>.yaml`，`spec.defaults` 寫成 `<name>/_defaults.yaml`，即 exporter 的子目錄 defaults 層：只套用到這個 CR 的租戶，且只送根 `_defaults.yaml` 或 `optional_overrides:` 宣告過的 key；沒宣告的 key、exporter 不套用的值由 `da-guard write` 拒寫。舊的 `<name>.yaml` 在同一批刪除。`stateFilters` 只在根 `_defaults.yaml` 有效、`profile` 從未被渲染，兩者改由 da-assembler 與 CRD 的 `x-kubernetes-validations` 拒收；要用請改寫在根 `_defaults.yaml`、各租戶的 `_profile`。CR 的 `lastRenderedHash` 改為涵蓋它所有渲染出的檔。
