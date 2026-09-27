---
section: Fixed
topic: dev-workflow
issues: [1996]
created: 2026-09-27T10:59:01+08:00
---
- **`pr_preflight` 的 CI 失敗不再附「本 PR 引入的，必須修」這類推斷（internal、dx；[#1996](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1996)）**：那句話取自 main 上任何一支 workflow 最新的一次 run，不代表 main 的 CI 結論，所以 main 本身也紅的時候，它照樣會叫你修。現在 CI status 只列出失敗的 check；要判斷是不是 main 原本就壞了，請自己看 main 上同名 check 的結果。FAIL／WARN 判定與 exit code 不變。
