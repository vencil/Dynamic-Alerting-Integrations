---
section: Fixed
topic: tools
issues: [4055]
created: 2026-11-22T10:57:45+00:00
---
- **輸出旗標給 `-`（寫到 stdout）而 stdout 寫不進去時，stderr 只剩一行（tools；[#4055](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4055)）**：例如 `-o - > /dev/full` 或 `-o - >&-`。先前只要輸出小於 stdout 的緩衝區（大輸出不受影響），`cannot write -` 那一行之後還會多一段 `Exception ignored` traceback 與第二行 `cannot write to stdout`。結束碼原本就是 2，沒有改變。
