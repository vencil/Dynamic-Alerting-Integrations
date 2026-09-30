---
section: Fixed
topic: lint-guards
issues: [1633]
created: 2026-09-30T08:54:17+08:00
---
- **隱式串接路徑守衛改用精確述詞，不再漏抓「3 個以上 fragment、收尾行有連續提及」（lint；[#1633](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1633)）**：`tests/ops/test_wrapped_path_references.py` 原本用「常數所在的那幾行」近似「`git grep` 會不會回傳這個站點」，路徑斷在前面幾個 fragment、收尾行另有連續寫法時會靜默。現在用 `tokenize` 把字串值的每個字元映射回它所在的原始碼行，再逐行比對；f-string 一併處理，連續寫的 f-string 路徑不會誤紅。映射自我檢查失敗時一律報紅並標為「無法映射」，不當成乾淨。判定在 Python 3.11 與 3.13 下相同。
