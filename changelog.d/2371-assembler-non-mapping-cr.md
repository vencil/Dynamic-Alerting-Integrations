---
section: Fixed
topic: confd-family
issues: [2371]
created: 2026-09-28T17:00:00+00:00
---
- **`da_assembler --render-cr` 遇到頂層不是 mapping 的 CR 改回 rc 2（da-tools；[#2371](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2371)）**：CR 檔頂層若是 list 或純量（例如整份寫成 `- kind: ThresholdConfig`），先前會丟 `AttributeError` traceback、rc 1，違反「rc 2 = caller error」的契約。現在與 `kind` 不符走同一條路徑：印一行 `is not a ThresholdConfig resource`，rc 2，不寫任何檔案。
