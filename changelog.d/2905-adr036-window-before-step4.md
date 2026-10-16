---
section: Changed
topic: exporter
issues: [2905]
created: 2026-10-16T13:57:49+00:00
---
- **ADR-036 §9 與 CLI 參考對齊 da-guard 現況，並寫明步驟 4 之前的已知窗口（文件；[#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)）**：`parse-tree` 判為讀不了、exporter 卻照讀的檔，da-guard 列為未判定證據（`generator_unreadable`，exit 4），不回 exit 3；先前文件寫成「由產生器那一側回報」，與行為不符。另寫明已知窗口：產生器改讀 `parse-tree` JSON 之前仍用 PyYAML，只有 PyYAML 拒收的寫法（同名錨點定義兩次、TAB 分隔、`2001-13-45` 這類不存在的日期、雙引號裡的 `\'`、`|#c`、flow 值以冒號結尾）放在沒有路由鍵的租戶檔裡時，da-guard 回 0、產生器卻整份拒收，所以 CI 必須同時跑 `da-tools generate-routes --validate --strict`。da-guard 行為不變；這個窗口由測試釘住。
