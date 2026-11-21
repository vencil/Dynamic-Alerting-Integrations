---
section: Changed
topic: docs
issues: [2789]
created: 2026-11-21T23:18:50+00:00
---
- **ADR-019 撤回六項從未實作的強化，同步檢查定案為只警告（docs；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：「已知限制」原本列為「本決策還要求、目前沒有實作」的六項改寫成決策本身：計畫狀態同步檢查只警告、不升級擋 merge、不在 PR 留言、不進 pre-push；索引不在 commit 時自動重產（pre-commit 只做 `--check`）；不做定期掃描（替代方案改為不採用並寫理由）；`CLAUDE.md` 只在路由表指向，不在起手式要求必讀索引。理由是 commit 實務上不寫 `Resolves`／`Closes`／`Fixes` trailer，追蹤項目的狀態已由 `TRK-<issue 號>` 即 GitHub issue 承擔。工具行為不變。
