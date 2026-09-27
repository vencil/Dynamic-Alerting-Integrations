---
section: Fixed
topic: dx
issues: [2141]
created: 2026-09-27T06:20:00+00:00
---
- **`pr_preflight` 的總結行只陳述量到的事（dx；[#2141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2141)）**：有警告時總結行不再說「可合併」，改為「沒有檢查失敗；N 項警告或未能判定（見上）」；沒有警告但有略過的項目時，不再說「所有檢查通過，可以 merge」，改為「沒有檢查失敗；M 項略過（見上）」。警告與略過可能是「量不到」（例如 gh 不可用、還沒開 PR），不能據此推出可以合併。只有全部通過時才說「所有檢查通過」。`BLOCKED`／`CAUTION`／`READY` 三個關鍵字與 exit code 不變。
