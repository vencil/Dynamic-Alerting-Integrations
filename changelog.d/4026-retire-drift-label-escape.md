---
section: Fixed
topic: tools
issues: [4026]
created: 2026-11-18T23:40:00+00:00
---
- **`check_retire_drift` 的文字報告把租戶 id 經 `safe_label` 跳脫（tools；[#4026](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4026)）**：`VIOLATION` 行的租戶 id 取自 conf.d、`WARN` 行的取自 helm 檔名或 namespace 的 `instance` label，先前原樣印出，帶換行時可以在第 0 欄偽造一行 `[PASS]`（[#1538](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1538) 那一類）。`--json` 輸出不變。
