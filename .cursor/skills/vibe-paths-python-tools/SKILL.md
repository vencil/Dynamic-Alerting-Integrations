---
name: vibe-paths-python-tools
description: "Vibe 路徑觸發指引：動到 scripts/tools/**/*.py 之前先讀的章節與一句約束。"
paths:
  - "scripts/tools/**/*.py"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-python-tools

先讀：
- `docs/internal/testing-playbook.md` §SAST 合規
- `docs/internal/dev-rules.md` §13. da-tools 子命令 exit-code
- `docs/internal/lint-policy.md` §7. 新增 lint 的審核 checklist

約束：新工具要守 exit code 0/1/2 與 `--ci`／`--json` 約定，`subprocess` 帶 timeout、`open` 帶 encoding；docstring 改了跑 `generate_tool_map.py --generate --lang all`；改到 `tests/shared/property-coverage.yaml` 已宣告的那幾個模組時，新 module-level 函式要進它的 triage（其他模組不受該 lint 管）。
