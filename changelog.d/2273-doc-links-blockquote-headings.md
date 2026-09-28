---
section: Fixed
topic: docs
issues: [2273]
created: 2026-09-28T06:12:18+00:00
---
- **`check_doc_links` 認得 blockquote 裡的標題（[#2273](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2273)）**：GitHub 會替 `> ### 標題` 產生錨點，這支 lint 原本不收，連到這種標題的正確連結會被報成壞連結。現在會先剝掉行首的 `>`（可巢狀）再判斷標題；blockquote 裡的 code fence 只在同一層 quote 內有效，quote 結束時 fence 跟著結束。
