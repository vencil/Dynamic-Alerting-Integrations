---
section: Changed
topic: e2e
issues: [1449, 3642]
created: 2026-11-06T17:22:30+00:00
---
- **E2E 停下的測試改由 runner 判定，且必須掛 issue（e2e；[#1449](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1449)）**：新增 `tests/e2e/reporters/parked-tests.ts`。依實際執行結果判斷：結果為 skipped，或預期狀態不是 passed（含 fixme、skip、fail、`testInfo.skip()`、別名、計算式呼叫），而且沒有 `#<n>` 形式的 `issue` annotation，整次執行判為失敗。ESLint 改為放行 `test.fixme`，仍擋所有 `.skip()`。reporter 清單只在 `playwright.config.ts` 列一處，CI 不再傳 `--reporter`。`_*` 草稿 spec 不再被 runner 收進來。`visual.spec.ts` 原本用 block comment 停著的 5 支測試，改為帶 [#3642](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3642) 的 fixme。frontend-quality-backlog 的手寫登記改為以 annotation 為準。
