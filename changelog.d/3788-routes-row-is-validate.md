---
section: Changed
topic: da-tools
issues: [3788]
created: 2026-11-13T16:30:00+00:00
---
- **⚠️ breaking：validate-config 的 `routes` 列就是 `generate-routes --validate`，`schema` 列移除（tools；[#3788](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3788)）**：兩者先前對同一棵樹可能給不同結論——租戶存在卻沒有產生任何 route／inhibit rule 時，`--validate` 結束碼 1，validate-config 結束碼 0。現在 `routes` 列與 `--validate` 是同一支判定，`--policy`、`--strict` 一併傳入，明細是 `--validate` 印出的內容。行為變化：`--json` 不再有 `schema` 列，它的內容都在 `routes` 列，讀 `schema` 列的 CI 請改讀 `routes`；上述那種樹 `routes` 列由 WARN 改為 FAIL、結束碼 0 → 1；路由面拒收的 conf.d 樹（例如子目錄有 `_routing_enforced`）結束碼 1 → 2，與 `generate-routes` 相同；`--validate` 也報的共用鍵判決與讀不了的租戶檔，現在同時讓 `routes` 列 FAIL（它們原本就讓 `unread_keys`、`yaml_syntax` 列 FAIL）。
