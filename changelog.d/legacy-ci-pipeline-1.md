---
section: Fixed
topic: ci-pipeline
issues: [1158, 1280, 1368, 1373, 1398, 1399, 1428, 1873, 2079]
created: 2026-09-26T17:00:00+00:00
---
- **required check 不再以 skip 過關或永遠 pending（ci）**：docs-ci 與 `validate.yaml` 拔掉 workflow 層 `paths:`，改由 `always()` 聚合 gate 回報 required check：不相關的 PR 不再因 check 從未回報而卡死 merge，被跳過的 job 也不再以 `skipped` 滿足 branch protection（[#1398](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1398)）。原本沒有 runner 的 `tests/e2e-bench/receiver` 測試、`bench_filter.go`、E2E spec lint（A-13）與出貨範例 conf.d 的 schema 檢查都接上 CI；移除從未生效的文件覆蓋率 badge auto-commit。
