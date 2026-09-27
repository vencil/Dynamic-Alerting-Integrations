---
section: Fixed
topic: dx
issues: [2140]
created: 2026-09-27T06:10:00+00:00
---
- **`pr_preflight` 的 CI status 只把 `pass` 算成通過（dx；[#2140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2140)）**：被取消（`cancel`）、缺 bucket 或 gh 回了不認得的 bucket 的 check，先前不計入任何一類，全部被取消時印「全部 0 個 checks 通過」並判 PASS，一過一取消時被取消的那個從訊息裡消失。現在這些 check 判 WARN，並逐一列在明細裡；沒有任何 check 通過（例如全部 `skipping`）時也判 WARN，不再說「全部 0 個通過」。`skipping` 仍是中性，有通過的 check 時照樣 PASS，訊息附上略過數。WARN 不擋 push，exit code 不變。
