---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-26T01:37:37+00:00
---
- **`generate_rule_pack_split` 在沒有 PyYAML 時也經寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：寫入函式改從只依賴標準函式庫的 `_lib_output` 載入，沒有 PyYAML 的那條降級路徑因此不再退回裸寫。該路徑上 `--output-dir` 不能用時，結束碼原本就是 `2`，但印的是 `NotADirectoryError` 的 traceback 加 `unexpected internal error`；現在是一行並點名 `--output-dir`。該路徑寫出的 `validation-report.json` 由 `0644`（依 umask）改為 `0600`，內容不變。有 PyYAML 的正常路徑輸出逐位元組不變。
