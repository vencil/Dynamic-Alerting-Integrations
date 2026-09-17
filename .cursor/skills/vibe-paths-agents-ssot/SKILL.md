---
name: vibe-paths-agents-ssot
description: "Vibe 路徑觸發指引：動到 .agents/** 之前先讀的章節與一句約束。"
paths:
  - ".agents/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-agents-ssot

先讀：
- `AGENTS.md`

約束：改完跑 `python3 scripts/tools/dx/gen_agent_adapters.py --generate`；`.claude/skills`、`.claude/agents`、`.cursor/skills/vibe-paths-*`、`.github/instructions/vibe-paths-*` 全是它的產物，`gen-agent-adapters-check` 擋漂移。
