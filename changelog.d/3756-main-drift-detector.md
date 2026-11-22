---
section: Added
topic: ci
issues: [3756]
created: 2026-11-21T23:30:00+00:00
---
- **main 上的 path filter 過期改由專屬 workflow 在幾分鐘內發現（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：新增 `.github/workflows/path-filter-drift.yaml`，每次 push 到 main 只跑 `workflow_path_filter.py generate --check`，不排主 CI 的隊。兩支各自同步的 PR 合入後可能讓生成的 `on.pull_request.paths` 區塊一起過期（一支讓某檔進入清單的 import closure，另一支在那個檔加 import）；以前要等主 CI 的 Python Tests 跑完才紅，追蹤 issue 可能在 main 壞掉一個多小時後才開。`main-red-detector.yml` 現在也監看這支 workflow。它只偵測、不預防；修法仍是在 PR 裡跑 `make workflow-path-filters`。
