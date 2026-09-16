---
name: vibe-paths-workflows
description: "Vibe 路徑觸發指引：動到 .github/workflows/** 之前先讀的章節與一句約束。"
paths:
  - ".github/workflows/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-workflows

先讀：
- `docs/internal/hook-vs-skill-coverage.md` §4.5 ⚙️ CI-only gates

約束：path-gated job 裡的 step 不得帶計算式 `if:`（條件寫進 shell）；pytest 讀到的 repo 檔要在 `python` filter 內——`tests/ops/test_ci_path_filter_coverage.py` 是那道守衛，且 fixture 檔名別用真實 repo 檔名。
