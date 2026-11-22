---
section: Changed
topic: ci
issues: [4224]
created: 2026-11-22T11:37:40+00:00
---
- **CI 每次 push 少開 job、少用 runner 分鐘（[#4224](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4224)）**：PR 的等待時間大半是排隊。所有在 PR 上觸發的 workflow 現在都會在同一 PR 有新 push 時取消舊 run，由 `tests/ops/test_pr_workflow_concurrency.py` 守住。
