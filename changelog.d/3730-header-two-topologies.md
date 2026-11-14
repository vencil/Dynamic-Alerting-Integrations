---
section: Fixed
topic: rule-packs
issues: [3730]
created: 2026-11-12T16:07:06+00:00
---
- **規則包 header 的 `_critical` 說明改為分別講清楚 Helm chart 與 repo dev conf.d 兩種拓撲（rule-packs；[#3730](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3730)）**：redis、mongodb、elasticsearch 的 9 個 `<base>_critical` 原本被歸為「documented-but-dormant、填了也不會發射」，但它們的 base 由 dev conf.d `_defaults.yaml` 的 `dev-example-defaults` 段供給，在 dev 樹裡會產出 critical row。header 現在多一組「base 只在 dev conf.d 範本供給、Helm chart 不出貨」，兩種拓撲各自寫明；分組由生成器依兩個生成面的同一組篩選條件判定，測試把它對回實際的 Helm values 與 dev `_defaults.yaml`。
