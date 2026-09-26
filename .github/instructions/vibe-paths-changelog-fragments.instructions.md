---
applyTo: "changelog.d/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-changelog-fragments

先讀：
- `changelog.d/README.md`
- `docs/internal/commit-convention.md` §Changelog fragments (`changelog.d/`)

約束：一個變更一個片段檔、本文恰好一個 `- ` 條目（≤ 1,000 字元）；修改還沒發布的功能要改它原本的片段，不要另開一份。驗：`python3 scripts/tools/dx/generate_changelog.py --fragments`（pre-commit `changelog-fragments`），預覽組裝結果：`--assemble`。
