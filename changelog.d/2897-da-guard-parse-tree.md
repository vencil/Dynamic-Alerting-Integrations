---
section: Added
topic: da-guard
issues: [2897]
created: 2026-10-11T11:06:25+00:00
---
- **`da-guard parse-tree`：以 JSON 輸出 conf.d 的解析結果（ADR-036 §2；[#2897](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2897)）**：列出 exporter 與路由產生器各讀哪些檔、每個目錄採用的 `_defaults`、每份檔經 yaml.v3 解碼後的內容（鍵為原文，純量帶原文、型別與值），以及讀不了的檔與其代號（封閉清單，例如 `duplicate_key`、`non_specific_tag`、`multiple_documents`；清單外的寫法照 yaml.v3 讀，即使今天的產生器拒收）。另有單檔模式（`--path` 加 `--input`）。讀不了的檔是資料，結束碼仍為 0；呼叫錯誤為 2。目前沒有讀取端，現有工具行為不變。
