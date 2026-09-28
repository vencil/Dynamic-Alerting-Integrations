---
section: Fixed
topic: lint-guards
issues: [1500]
created: 2026-09-28T21:00:00+08:00
---
- **折行路徑守衛不再把句尾單字接上下一行檔名誤判為折行參照（lint；[#1500](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1500)）**：`tests/ops/test_wrapped_path_references.py` 原本只要新增一個檔名湊巧吻合的檔案（例如空的 `merged_groups.yaml`），就會讓別處一段沒動過的英文註解轉紅。現在三種形狀不再接合：前半段是不含 `/ - _ .` 的普通單字、目錄樹列表（`dir/` 之後縮排更深的成員）、以 `- ` 開頭的新列表項目。代價是少數真實折行（檔名在第一個無分隔字元的段落內被切開、`/` 後的懸掛縮排）也不再被抓到，已寫進守衛 docstring。
