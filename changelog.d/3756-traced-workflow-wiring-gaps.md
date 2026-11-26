---
section: Fixed
topic: ci
issues: [3756]
created: 2026-11-26T09:20:55+00:00
---
- **`workflow_path_filter.py generate --check` 擋下讓讀檔斷言不跑、或 trace 不上傳的接線（[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：traced workflow 的 job 不得有 job 層 `if:`；`on.pull_request` 只能用 `paths` 收窄（不得有 `types`／`branches`），且不得有 `pull_request_target`（它跑在 base 上，量到的是錯的樹）；matrix 只能是非空的字面 `include` 清單；斷言步驟之後恰好一個上傳步驟，上傳斷言的 `--out`，`if:` 與斷言步驟相同；任何 `env:` 不得設 `GIT_CONFIG*`（可在 `run:` 文字之外改掉 `core.trustctime`），workflow 與 job 的 `env:` 不得設 `PYTHON*`。這幾種寫法原本都能通過 `generate --check`。
