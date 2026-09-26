---
name: vibe-paths-python-tests
description: "Vibe 路徑觸發指引：動到 tests/**/*.py 之前先讀的章節與一句約束。"
paths:
  - "tests/**/*.py"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-python-tests

先讀：
- `docs/internal/test-map.md` §測試基礎設施

約束：掃 repo 檔用 `tests/_tree.py` 的 `repo_files()`，不要 `REPO_ROOT.rglob`（會把 `.claude/worktrees` 裡的 repo 副本當成本樹）；本地驗證新 CLI 工具要跑 `pytest tests/`，管它的契約測試在 `tests/shared/`、`tests/ops/`，不在工具自己那層。
