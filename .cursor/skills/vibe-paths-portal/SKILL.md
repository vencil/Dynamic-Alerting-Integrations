---
name: vibe-paths-portal
description: "Vibe 路徑觸發指引：動到 tools/portal/** 之前先讀的章節與一句約束。"
paths:
  - "tools/portal/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-portal

先讀：
- `tools/portal/CLAUDE.md`
- `docs/internal/jsx-multi-file-pattern.md` §Build & verify loop

約束：`docs/assets/dist/` 是 build 產物、不進 git（#3738）：commit 只含 source，本機要看結果再 `make portal-build`；`make test-portal` 跑 Vitest；Windows host 要在 worktree 內 build。
