---
section: Added
topic: ci
issues: [2005]
created: 2026-09-27T11:55:26+00:00
---
- **測試執行時擋下產品碼沒帶 encoding 的讀寫檔（tests；[#2005](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2005)）**：`scripts/`、`components/` 的程式在測試中呼叫 `read_text`／`write_text`／`Path.open`／`open` 卻沒帶 `encoding=` 時，該測試會失敗；測試檔 import、fixture setup／teardown 也算在內。這類呼叫靜態檢查看不出來，改由 `PYTHONWARNDEFAULTENCODING=1` 產生的 `EncodingWarning` 判定。CI 的 Python Tests 會開啟它；沒開時本機照跑，結尾印出 `encoding gate: INACTIVE`。`subprocess(text=True)` 與在子行程裡跑的程式不在範圍內。`analyze_probe` 讀檔改為明確用 UTF-8。
