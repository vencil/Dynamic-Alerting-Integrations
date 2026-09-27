---
section: Fixed
topic: dx
issues: [2205]
created: 2026-09-27T22:00:00+00:00
---
- **diff 掃描型 lint 改以 merge base 為比對基準（lint；[#2205](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2205)）**：`_lint_helpers.resolve_diff_base()` 原本回傳 `origin/main`（或 `$LINT_DIFF_BASE`／`origin/$GITHUB_BASE_REF`）本身，分支落後 main 時，main 之後改掉的舊內容會被當成這個分支新增的違規。現在改回傳該 ref 與 HEAD 的 merge base；找不到 merge base（例如淺 clone 深度不足）時回 rc 2 並提示加深 fetch。CI 的 PR checkout 是合併 commit，比對結果不變。`generate_changelog --lint` 的 `[Unreleased]` 成長上限一併改用 merge base，訊息仍標出來源 ref。
