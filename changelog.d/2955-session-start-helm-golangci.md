---
section: Changed
topic: ci
issues: [2955]
created: 2026-10-13T16:33:56+00:00
---
- **remote session 起手式改為安裝釘版的 helm 與 golangci-lint（dx；[#2955](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2955)）**：`.claude/hooks/session-start.sh` 依 dev container 的版本下載 helm 與 golangci-lint，以 repo 內釘住的 sha256 驗證後安裝，失敗時 marker 寫 `RESULT=failed (<工具>: <原因>)`。remote session（`CLAUDE_CODE_REMOTE=true`）比照 CI 的 `VIBE_REQUIRE_HELM=1`：沒有 helm 時 helm 相關測試是失敗而非整批略過。`test_toolchain_pin_parity.py` 新增 session-start 與 dev container 的版本對帳。
