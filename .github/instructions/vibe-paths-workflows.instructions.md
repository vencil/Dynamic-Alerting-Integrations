---
applyTo: ".github/workflows/**"
---
<!-- 此檔為產生物，來源 .agents/paths-map.json —— 請改那份 SSOT，再跑 `make agent-adapters`；不要直接編輯這份複本。 -->
# vibe-paths-workflows

先讀：
- `docs/internal/agent-rulebook-shapes.md` §D-04 有 gate ≠ gate 會跑
- `tests/ops/test_ci_path_filter_coverage.py`

約束：path-gated job 裡的 step 不得帶計算式 `if:`（條件寫進 shell）；pytest 讀到的 repo 檔要在 `python` filter 內（且進 `PYTHON_ENTRIES_THIS_SCANNER_JUSTIFIES`）——`tests/ops/test_ci_path_filter_coverage.py` 是那道守衛，其 docstring 列了它拒收與看不見的形狀；fixture 檔名別用真實 repo 檔名。
