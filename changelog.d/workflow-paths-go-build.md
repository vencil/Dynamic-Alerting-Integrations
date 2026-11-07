---
section: Fixed
topic: ci
issues: [3601]
created: 2026-11-07T10:51:30+08:00
---
- **只改 `scripts/ops/go_build.sh` 的 PR 也會觸發 Defaults Impact Guard 與 Config Diff Advisory（ci；[#3601](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3601) 衍生）**：兩支 workflow 都以它建出受測的 da-guard，先前它不在 `on.pull_request.paths` 裡，改 build flag 的 PR 不會跑這兩支。`WORKFLOW_PATHS_PENDING` 對應的兩筆移除。
