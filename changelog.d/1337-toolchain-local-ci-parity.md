---
section: Changed
topic: ci
issues: [1337]
created: 2026-09-28T11:35:45+00:00
---
- **本地與 CI 的工具鏈版本收斂為同一套（ci / dx；[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)）**：所有 workflow 改用 Node 24、Python 3.13、`ubuntu-24.04` runner，同一個 action 只留一個版本；trivy、helm 從「跟著 action 預設值走」改為明確釘版，dev container 改為 Ubuntu 24.04 並把 Node、kubectl、helm、kind 從 `latest`/`lts` 改為與 CI 相同的明確版本，另新增 trivy 安裝，`make trivy-scan-all` 在本地 trivy 與 CI 不同版時會提示。docs-ci 的 promtool／yq 與 pre-commit 的 shellcheck 升到最新穩定版。各工具的 SSOT、消費端與守衛見 [工具鏈版本對照](docs/internal/toolchain-versions.md)，由新的 `tests/shared/test_toolchain_pin_parity.py` 把關。⚠️ 既有 dev container 需要 rebuild 才會套用。
