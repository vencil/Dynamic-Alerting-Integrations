---
section: Fixed
topic: alertmanager-routing
issues: [2991]
created: 2026-10-15T16:36:00+00:00
---
- **da-guard 在沒有租戶的 scope 上不再一律回 vacuously safe（[#2991](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2991)）**：`--scope` 內沒有租戶時，da-guard 先前直接印「vacuously safe」、rc 0，不管 defaults 檔的 `root_defaults_unwrapped`、`defaults_toplevel_ignored`、`root_defaults_critical_key`，也不管整棵樹的路由內容；同一棵樹加一個租戶就會報。現在零租戶也照常跑所有檢查，有 finding 就出一般報告（租戶數 0），依 finding 決定結束碼（warning 照例只在 `--warn-as-error` 下回 1）；樹上有路由或 domain policy 時回 exit 4「未判定」（[#2904](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2904)，交給 `generate-routes --validate --strict`）；兩者都沒有才是 vacuously safe、rc 0。
