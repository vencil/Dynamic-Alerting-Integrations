---
name: vibe-paths-workflows
description: "Vibe 路徑觸發指引：動到 .github/workflows/** 之前先讀的章節與一句約束。"
paths:
  - ".github/workflows/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-workflows

先讀：
- `docs/internal/agent-rulebook-shapes.md` §D-04 有 gate ≠ gate 會跑
- `tests/ops/test_ci_aggregate_gate_contract.py`

約束：回報必要檢查的 workflow（`ci.yml`／`docs-ci.yaml`／`validate.yaml`）不設 path gate（#3756）：不加 `dorny/paths-filter` 步驟；aggregate gate 等待的 job 與它 `needs:` 的上游不得帶 `if:`，其 step 的 `if:` 只接受 `always()`（條件寫進 shell）；gate 只在每條 leg 都是 `success` 時通過，不得綁 `needs.*.outputs.*`——`tests/ops/test_ci_aggregate_gate_contract.py` 是那道守衛，其 docstring 列了它看不見的形狀。`on.pull_request.paths` 裡 `# BEGIN/END GENERATED path-trace` 之間由 `make workflow-path-filters` 產生、不手改（#3756）；filter 的 pattern 比對不到任何 tracked 檔會被 `workflow-path-filter-check` 擋下。
