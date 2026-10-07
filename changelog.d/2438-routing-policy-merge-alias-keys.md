---
section: Fixed
topic: confd-family
issues: [2438, 2437, 2677]
created: 2026-09-30T00:34:28+00:00
---
- **da-guard 現在會執行經 YAML merge key 帶入的 domain policy，alias key 的名稱與產生器一致（[#2438](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2438)、[#2437](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2437)、[#2677](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2677)）**：`_domain_policy.yaml` 以 `<<: *anchor` 帶入的 domain、policy 或 `constraints`，da-guard 先前不讀，現在與產生器一樣執行；`routing_profiles:` 下與檔案最上層的 `<<:` 也會展開。da-guard 的取值優先序與產生器（PyYAML）相同：明寫的鍵最優先；同一層多個 merge key（如 `!!merge x:` 加 `<<:`）後出現者勝；同一個 merge 序列內先出現者勝；指向 anchored `<<` 的 alias key 也是 merge key；tenant-api 讀路由設定時也依此優先序。merge 的值不是 mapping 或 mapping 清單（如 `!!merge q: 5`），或 `<<` 寫在值的位置（`tenants` 清單的項目除外，產生器照原文讀）時，da-guard 與產生器一樣整份不讀。tenant-api 讀 `_domain_policy.yaml` 時，遇到上述整份不讀的情形（`require_critical_escalation` 原地寫在 domain `constraints` 下且值內無 anchor 時只關掉該條，經 merge 或 alias 帶入則整份不採用）或重複的鍵，或任何位置有產生器當 merge key、yaml.v3 當普通鍵的 key（如 `!!merge q:`、指向 anchored `<<` 的 alias）時，整份不採用。其他 alias key 的名稱一律取 anchored 文字。
