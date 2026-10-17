---
section: Changed
topic: exporter
issues: [2905]
created: 2026-10-16T13:57:49+00:00
---
- **ADR-036 §9 與 CLI 參考對齊 da-guard 現況（文件；[#2905](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2905)）**：`parse-tree` 判為讀不了、exporter 卻照讀的檔，da-guard 列為未判定證據（`generator_unreadable`，exit 4），不回 exit 3，交給產生器判；產生器改讀 `parse-tree` 後（[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）這類檔一律由它整份拒收。先前文件寫成「由產生器那一側回報」，與行為不符。da-guard 行為不變。
