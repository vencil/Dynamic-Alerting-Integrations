---
section: Fixed
topic: alertmanager-routing
issues: [2991]
created: 2026-10-15T16:36:00+00:00
---
- **da-guard 在沒有租戶的 scope 上也回報不依賴租戶的問題（[#2991](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2991)）**：`--scope` 內沒有租戶時，da-guard 先前直接印「vacuously safe」、rc 0，不管整棵樹的平台檔問題（例如 `_routing_defaults: [1, 2]`、子目錄的 `_routing_enforced`、重複的 routing profile 或租戶 id）、根目錄 `_routing_enforced` 不是字串的 `group_by`（產生器 `--validate --strict` 照樣擋），也不管 defaults 檔的 `root_defaults_unwrapped`、`defaults_toplevel_ignored`、`root_defaults_critical_key`；同一棵樹加一個租戶就會報。現在零租戶也照常跑所有檢查，有 finding 就出一般報告（租戶數 0），依 finding 決定結束碼（warning 照例只在 `--warn-as-error` 下回 1）；沒有任何 finding 才是 vacuously safe、rc 0。`{{tenant}}` 形的 `_routing_enforced` 沒有租戶就不產生 route，兩邊都不報。連帶：validate-config 的 profiles 列取 da-guard 的 `routing_in_unread_location`，所以沒有租戶的樹裡 profile 帶 `_routing` 現在也 FAIL。
