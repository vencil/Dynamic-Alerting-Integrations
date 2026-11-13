---
section: Changed
topic: da-tools
issues: [2294]
created: 2026-09-28T12:50:00+00:00
---
- **da-tools 映像內含 `amtool`（da-tools；[#2294](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2294)）**：`explain-route --trace` 由 `amtool config routes test` 判定路由，`generator-service` 在 PATH 上沒有 `amtool` 時回 503。映像的 `/usr/local/bin/amtool` 取自部署清單釘住的同一個 Alertmanager image（tag 與 digest 皆同，由測試守住一致、Renovate 同一支 PR 一起升版），上游的 Apache-2.0 LICENSE／NOTICE 放在 `/usr/share/doc/amtool/`。
