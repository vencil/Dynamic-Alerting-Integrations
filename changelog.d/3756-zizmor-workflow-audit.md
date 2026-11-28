---
section: Added
topic: ci
issues: [3756]
created: 2026-11-28T16:13:17+00:00
---
- **GitHub Actions 安全稽核改由 zizmor 把關（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：新增 pre-commit hook `zizmor`（離線、regular persona），CI 的 Lint job 點名執行，只開 `excessive-permissions`、`secrets-inherit`、`overprovisioned-secrets` 三項（`.github/zizmor.yml`）。接入時全部修到零：`ci.yml`、`commitlint.yaml` 補上頂層 `permissions: contents: read`；`bench-gate-pr`、`release`、`try-local-smoke` 的 write 權限由 workflow 層移到實際使用的 job；`visual-baseline` 的 `inputs.spec`／`inputs.reason` 改經 `env:` 傳入，不再直接展開進 `run:`。
