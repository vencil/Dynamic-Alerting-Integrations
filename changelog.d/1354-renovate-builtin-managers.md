---
section: Changed
topic: ci
issues: [1354]
created: 2026-09-28T16:07:52+00:00
---
- **Renovate 加開 github-actions、dockerfile、devcontainer 三個內建 manager（ci；[#1354](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1354)）**：每週一次，每個 manager 各一支 grouped PR；major 一律要在 Dependency Dashboard 打勾。會讓 CI 綁定守衛只升一半的 pin 明確排除、留給人手升：`golang:*` builder（SSOT 是 `go.mod`）、`runs-on` 與 dev container base image、action `with:` 內的工具版本、dev container 的 node／go／python 版本，以及已由 custom.regex 管理的 amtool `COPY --from`。`tests/**` 維持在 Renovate 範圍外，docker-compose 與 gomod 不開。內建 manager 不做 digest／SHA pin。歸屬表見 `docs/internal/toolchain-versions.md`。
