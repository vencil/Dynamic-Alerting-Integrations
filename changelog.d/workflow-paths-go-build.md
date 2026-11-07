---
section: Fixed
topic: ci
issues: [3601]
created: 2026-11-07T10:51:30+08:00
---
- **只改 `scripts/ops/go_build.sh` 的 PR 也會觸發 Defaults Impact Guard 與 Config Diff Advisory（ci；[#3601](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3601) 衍生）**：兩支 workflow 都以它建出受測的 da-guard，先前它不在 `on.pull_request.paths` 裡，改 build flag 的 PR 不會跑這兩支。bench 三筆依 owner 裁決改列「刻意不觸發」，每筆點名實際在 PR 上檢查它的對象並由測試驗證：`bench_interleave.sh` 經 `bench_gate_compare.sh` 由 `bench-gate-pr.yaml` 執行、`bench_gate_compare.sh` 由 `bench-gate-pr.yaml` 直接執行、`pair_bench_ratio.py` 由其單元測試涵蓋（`bench-workload-effect` 的 self-test 在換參考版本 #2348 前跑不到這兩支腳本）。代觸發不再因該腳本啟動、執行它的 step／job／`needs` 上游帶 `if:` 或 `continue-on-error`、間接呼叫只剩註解、單元測試不再 import 該模組，或項目已被涵蓋，都會轉紅。`WORKFLOW_PATHS_PENDING` 清空。
