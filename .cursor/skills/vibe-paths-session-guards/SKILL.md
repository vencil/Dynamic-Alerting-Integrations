---
name: vibe-paths-session-guards
description: "Vibe 路徑觸發指引：動到 scripts/session-guards/**、.claude/settings.json 之前先讀的章節與一句約束。"
paths:
  - "scripts/session-guards/**"
  - ".claude/settings.json"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-session-guards

先讀：
- `docs/internal/hook-vs-skill-coverage.md` §2. PreToolUse session-guards

約束：hook 命令一律經 `run-hooks.sh`（裸 `python` 在 Windows 是 Store stub，#824）；新 hook 的 AC 必含 live-fire 證據；`check_session_guard_liveness.py --ci` 在 commit 時驗接線與直譯器。
