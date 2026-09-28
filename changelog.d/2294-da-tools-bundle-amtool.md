---
section: Changed
topic: da-tools
issues: [2294]
created: 2026-09-28T12:50:00+00:00
---
- **da-tools 映像內含 `amtool`，`generate-routes` 在映像裡預設就經 Alertmanager 驗證（da-tools；[#2294](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2294)）**：`da-tools init --ci` 產生的 CI 範本都在 da-tools 映像裡跑 `generate-routes`，而映像以前沒有 `amtool`，所以客戶端預設不經 Alertmanager 自己的 parser 驗證。現在映像的 `/usr/local/bin/amtool` 取自部署清單釘住的同一個 Alertmanager image（tag 與 digest 皆同，由測試守住一致、Renovate 同一支 PR 一起升版），上游的 Apache-2.0 LICENSE／NOTICE 放在 `/usr/share/doc/amtool/`。
