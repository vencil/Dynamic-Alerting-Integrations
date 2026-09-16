---
applyTo: "tests/e2e/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-playwright-e2e

先讀：
- `docs/internal/testing-playbook.md` §Playwright E2E 測試

約束：`playwright-lint` pre-commit 需要 `tests/e2e/node_modules`（web session 由 session-start hook 裝）；spec 內的 `test.fixme()` 有治理規則，別拿它當跳過。
