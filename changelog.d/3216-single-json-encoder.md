---
section: Changed
topic: da-tools
issues: [3216, 3144, 3107]
created: 2026-10-30T12:52:44+00:00
---
- **`--json` 文件改由同一個出口寫出（da-tools；[#3216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3216)）**：工具交回文件，由 `_lib_exitcodes.run` 以唯一的編碼器寫出（兩格縮排、UTF-8、日期為 ISO 8601、YAML 未加引號的日期為原文）。**Breaking**：文件含 NaN／Infinity（含 conf.d 寫成 `.inf`／`.nan` 的值）或其他非 JSON 型別時，不再印出不合法的 JSON，改為 rc 2，envelope 的 reason 為 `output_not_serializable` 並指出 JSON 路徑。本次遷移 explain-route：conf.d 有未加引號的日期時，`--json` 不再 crash（[#3144](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3144)，rc 2→0）；其餘工具分批遷移。config-history 的快照與 baseline 的 CSV 改由同一個輸出寫入器設 0600（[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）。
