---
section: Fixed
topic: docs
issues: [2234]
created: 2026-09-28T01:28:29+00:00
---
- **glossary 與 da-portal README 不再寫手寫計數（docs；[#2234](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2234)）**：詞彙表中英兩版把告警速查的連結描述為「96 個告警」，而 `rule-packs/ALERT-REFERENCE.md` 實際列出 119 個；da-portal README 寫「44 個工具」，而 `tools/portal/manifest.json` 有 45 個。兩個數字都沒有機制維持，改為不帶數字，工具清單以 `manifest.json` 與 `tool-registry.yaml` 為準。
