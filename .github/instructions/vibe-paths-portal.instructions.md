---
applyTo: "tools/portal/**,docs/assets/dist/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-portal

先讀：
- `docs/internal/jsx-multi-file-pattern.md` §Build & verify loop

約束：`make portal-build` 後 dist 必須與 source 一致（`dist-source-consistency-check`）；`make test-portal` 跑 Vitest；Windows host 要在 worktree 內 build，別讓 main checkout 的 dist 混進來。
