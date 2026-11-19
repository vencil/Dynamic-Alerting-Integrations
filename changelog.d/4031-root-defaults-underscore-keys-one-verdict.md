---
section: Changed
topic: tools
issues: [4031]
created: 2026-11-19T02:30:00+00:00
---
- **⚠️ 根 `defaults:` 的 `_` 鍵只剩 da-guard 一份判決，validate-config 不再出兩列相反的修法（tools；[#4031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4031)）**：`root_defaults` 列不再判 `_` 鍵（原本是 #3045 的 WARN）。登錄表沒有的鍵、merge 會丟掉的 `_metadata`，改由鍵判決在 `routes` 列以 error 報在該檔上，原因 `unregistered`／`merge_dropped`；沒有租戶時也報，所以這種樹的 validate-config、`generate-routes --validate`、`da-guard write` 會從 rc 0 變 1。登錄表上的鍵維持由 `guard_errors` 報。
