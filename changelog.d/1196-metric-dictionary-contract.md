---
section: Fixed
topic: da-tools
issues: [1196]
created: 2026-09-27T23:38:43+00:00
---
- **metric-dictionary 只再指向 rule pack 真的在讀的 key 與存在的告警（da-tools；[#1196](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1196)）**：`migrate` 會把字典的 `maps_to` 當成「請設定這個閾值」、`golden_rule` 當成「改用這條黃金標準」告訴使用者。先前 13 個 `maps_to` 沒有任何告警讀（多半是固定條件的黃金規則），10 個 `golden_rule` 不是存在的告警名，例如 `ContainerHighCPU` 實為 `PodContainerHighCPU`。這些已逐條改正，固定條件的改寫 `maps_to: null`，migrate 報告也不再叫人去設不存在的閾值。`metric-dictionary-check` 把這三件事列為 error。`generate_tenant_mapping_rules` 改用字典的原始指標名：先前拿 `maps_to` 當 series 名，產出的 recording rule 恆為空，遇到 `maps_to: null` 還會崩潰。
