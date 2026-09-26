---
section: Fixed
topic: ci-pipeline
issues: [1158, 1280, 1368, 1373, 1398, 1399, 1428, 1873, 2079]
created: 2026-09-26T17:00:00+00:00
---
- **required check 不再以 skip 過關或永遠 pending（ci）**：docs-ci 與 `validate.yaml` 拔掉 workflow 層 `paths:`，改由 `detect-changes` 加 `always()` 聚合 gate 回報 required check：不相關的 PR 不再因 check 從未回報而卡死 merge，detect 失敗也不再以 `skipped` 滿足 branch protection（[#1398](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1398)）；`Python Tests (3.13)` 的 filter 加上 catch-all，避免該跑的測試被判成不需要。path filter 補齊多處缺口（pin SSOT `requirements/ci-constraints.txt`、mkdocs hook、`CHANGELOG.md`、gated job 實際執行或讀取的檔），並有守衛掃描 gated job 執行／安裝的檔，Go 測試則在執行當下核對實際開過的檔（[#1399](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1399)）。原本沒有 runner 的 `tests/e2e-bench/receiver` 測試、`bench_filter.go`、E2E spec lint（A-13）與出貨範例 conf.d 的 schema 檢查都接上 CI；移除從未生效的文件覆蓋率 badge auto-commit。
