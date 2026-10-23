---
section: Added
topic: confd-family
issues: [3206]
created: 2026-10-23T18:30:00+00:00
---
- **產生器容器一次完整判定的預算量測工具（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：`scripts/tools/dx/measure_judgement_budget.py` 在沒有逐檔快取的路徑上跑完一次判定（從 git 物件取檔、`parse-tree` 整棵樹模式、`generate-routes --validate --strict` 含 amtool、`served-values --tenant`），以 `RLIMIT_DATA` 與釘住的 CPU 跑多次，逐支程式報峰值記憶體與時間，對照 ADR-036 §3「快取的去留」的條件。記憶體上限、判定期限與 CPU 數都從 Helm chart 渲染結果與 generator-service 自己的參數解析取得，不另寫數字。維運內部工具，不影響部署。
