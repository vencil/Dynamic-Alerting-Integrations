---
section: Added
topic: ci
issues: [3799]
created: 2026-11-12T23:53:29+00:00
---
- **`make renovate-fixup`：Renovate 第三方 image PR 不再要逐項找連動修改（[#3799](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3799)）**：在 PR 分支上跑一次，就依分支上已升好的部署 ref 補齊 Renovate 寫不到的地方：da-read 的 go.sum、`tests/oauth2-proxy` 的函式庫版本、federation-e2e 與 try-local compose 的 image、platform-data、portal 部署精靈的 `IMAGE_MIRROR` 與 dist。Renovate 開出的群組 PR 本文會提示這一步。CI 權限不變；跑完仍紅的只剩升版模組本身的 API 變更，要人修。`--dry-run` 只列出會改什麼。
