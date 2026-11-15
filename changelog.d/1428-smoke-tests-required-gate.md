---
section: Fixed
topic: ci
issues: [1428]
created: 2026-11-07T15:23:26+00:00
---
- **Smoke Tests (Chromium) 改成每張 PR 都會回報的 required check（ci；[#1428](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1428)）**：Playwright smoke 從 `playwright.yml` 移到 `ci.yml`，拆成每支 PR 都執行的 `smoke-tests-run`，以及名稱維持 `Smoke Tests (Chromium)` 的 `if: always()` aggregate gate，做法與 Portal Tests 相同。原本整個 workflow 被頂層 `paths:` 擋掉，沒碰 portal 的 PR 收不到這個 required check、永遠卡在 pending。現在 gate 只在 `smoke-tests-run` 成功時通過。`playwright.yml` 只剩 spec lint（A-13）。
