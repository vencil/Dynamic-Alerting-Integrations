---
section: Fixed
topic: alertmanager-routing
issues: [2991]
created: 2026-10-15T16:36:00+00:00
---
- **da-guard 在沒有租戶的 scope 上也回報平台檔的 routing 問題（[#2991](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2991)）**：`--scope` 內沒有租戶時，da-guard 先前直接印「vacuously safe」、rc 0，連整棵樹讀到的平台檔問題（例如 `_defaults.yaml` 寫 `_routing_defaults: [1, 2]`、子目錄的 `_routing_enforced`、重複的 routing profile、重複的租戶 id）也一併略過；同一份檔加一個租戶就 rc 1。現在只要有這類 finding 就照常出報告（租戶數 0）：error 級回 1，`routing_profiles_unusable` 是 warning，照例只在 `--warn-as-error` 下回 1。沒有任何問題的全新目錄骨架仍是 rc 0。
