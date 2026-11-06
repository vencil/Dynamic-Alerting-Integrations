---
section: Fixed
topic: ci
issues: [3601]
created: 2026-11-05T23:54:43+00:00
---
- **只改 `guard_defaults_scopes.py` 的 PR 現在會觸發 Dangling Defaults Guard（ci；[#3601](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3601)）**：`guard-defaults-impact.yml` 會執行這支 helper，但 `on.pull_request.paths` 沒列它，helper 改壞了 CI 也看不到。現已補上，並在 `tests/ops/test_ci_path_filter_coverage.py` 新增 invariant 4：凡以 `on.pull_request.paths` 自我閘控的 workflow，其步驟執行的 `scripts/**` 都必須在那份 paths 裡；解析不了的 workflow 會列出並轉紅。其他 5 筆同形狀的缺口（4 支 workflow）列在只准變短的待判定清單中。
