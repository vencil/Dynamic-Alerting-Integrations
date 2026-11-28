---
section: Fixed
topic: portal
issues: [4290]
created: 2026-11-28T11:00:00+00:00
---
- **jsx-loader 只用 `data-theme` 切 dark mode，JSX 的 `dark:` 改由 pre-commit 擋（portal；[#4290](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4290)）**：loader 不再設 Tailwind 的 `darkMode: 'class'`，也不再替 `<html>` 加 class `dark`；外框樣式（返回連結、主題按鈕、載入動畫、流程提示與導覽列）改用 `html[data-theme="dark"]` 選擇器，畫面不變。新的 pre-commit hook `jsx-no-dark-variant` 擋 `tools/portal/src/` 底下 `.jsx` 裡的 `dark:` 變體，CI 的 Lint job 也跑它，符合 ADR-015 只有一套 dark mode 機制的決策。
