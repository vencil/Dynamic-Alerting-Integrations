---
name: vibe-paths-changelog
description: "Vibe 路徑觸發指引：動到 CHANGELOG.md 之前先讀的章節與一句約束。"
paths:
  - "CHANGELOG.md"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-changelog

先讀：
- `docs/internal/commit-convention.md` §Editing `CHANGELOG.md` by hand
- `docs/internal/hook-vs-skill-coverage.md` §3. Pre-commit auto hooks

約束：`## [Unreleased]` 已凍結（#2102）：新增條目會被 `changelog-format` 擋下，還沒發布的變更改寫成 `changelog.d/` 片段檔（格式見 `changelog.d/README.md`）。只剩發版收尾與修正既有條目會動這個檔；驗：`python3 scripts/tools/dx/generate_changelog.py --base origin/main --lint CHANGELOG.md`。rebase／解衝突碰到這個檔，之後跑 `python3 scripts/tools/dx/changelog_rebase_check.py`：條目被靜默吃掉或重複時，數量常常不變。
