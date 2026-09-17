---
name: vibe-paths-generated-adapters
description: "Vibe 路徑觸發指引：動到 .claude/skills/**、.claude/agents/**、.cursor/skills/vibe-paths-*/**、.github/instructions/vibe-paths-* 之前先讀的章節與一句約束。"
paths:
  - ".claude/skills/**"
  - ".claude/agents/**"
  - ".cursor/skills/vibe-paths-*/**"
  - ".github/instructions/vibe-paths-*"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-generated-adapters

先讀：
- `AGENTS.md`

約束：⛔ 這是產生物：改 `.agents/` 的來源再重生；直接改這裡會被 `gen-agent-adapters-check` 擋下，下次 `--generate` 也會覆蓋（同目錄裡非 `vibe-paths-` 前綴的檔不是產物，產生器不碰）。
