---
section: Changed
topic: ci
issues: [4224]
created: 2026-11-22T11:37:40+00:00
---
- **CI 每次 push 少開 job、少用 runner 分鐘（[#4224](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4224)）**：PR 的等待時間大半是排隊。所有在 PR 上觸發的 workflow 現在都會在同一 PR 有新 push 時取消舊 run，由 `tests/ops/test_pr_workflow_concurrency.py` 守住。只等一個 job 的 aggregate gate（validate 3 個、docs-ci 6 個、Portal、Smoke）刪除，該 job 直接回報同名的必要檢查（`kind: job`），必要檢查名稱不變。Python 測試只跑一遍：coverage 改走 `COVERAGE_CORE=sysmon` 併進必要的測試分片，刪掉另一組 advisory coverage 分片；`Python Tests` 在三片都成功後合併 coverage，低於 `fail_under = 75` 時⚠️ 必要檢查轉紅（原本紅了不擋）。秒級的 6 個 Go 小模組併成一個 job。
