---
applyTo: "CHANGELOG.md"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-changelog

先讀：
- `docs/internal/hook-vs-skill-coverage.md` §3. Pre-commit auto hooks

約束：只動 `## [Unreleased]`；整段對 base 每新 bullet ≤ 1,000 字元，量測數字放 commit／issue 不放這裡：`python3 scripts/tools/dx/generate_changelog.py --base origin/main --lint CHANGELOG.md`。
