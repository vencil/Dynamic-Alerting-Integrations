---
name: vibe-paths-portal
description: "Vibe 路徑觸發指引：動到 tools/portal/**、docs/assets/dist/** 之前先讀的章節與一句約束。"
paths:
  - "tools/portal/**"
  - "docs/assets/dist/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-portal

先讀：
- `docs/internal/jsx-multi-file-pattern.md` §Build & verify loop

約束：改 source 後自己跑 `make portal-build` 重生提交的 dist——「改了 source 忘了重 build」沒有任何 hook 擋（`dist-source-consistency-check` 只抓反向：提交了 dist 卻沒有 source 變更）；`make test-portal` 跑 Vitest；Windows host 要在 worktree 內 build。
