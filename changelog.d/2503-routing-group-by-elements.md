---
section: Changed
topic: alertmanager-routing
issues: [2503]
created: 2026-10-01T14:52:38+00:00
---
- **`group_by` 的元素未加引號讀成非字串、空字串、重複，或 `...` 與其他 label 並存時，`--strict`、da-guard、tenant-api 改為報錯（[#2503](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2503)）**：例如 `group_by: [alertname, on, 8]`，原本產生器原樣輸出、`--dry-run --strict` 與 `--validate --strict` 都是結束碼 0，Alertmanager 卻以名叫 `true`／`8` 的 label 分組；`[alertname, alertname]`、`[alertname, "..."]`、`[alertname, ""]` 則整份 config 被 Alertmanager 拒收。涵蓋租戶主 route、`overrides[i]`、`routes[i]`（含 `_routing_defaults`／routing profile 繼承來的值）與 `_routing_enforced`（只判會輸出的 route，`{{tenant}}` 形狀逐租戶代換後判）。`generate-routes --strict` 與 `validate-config --strict` 每個元素一行 `ERROR`、結束碼 1；da-guard 報 `routing_group_by_invalid`（error；`_routing_enforced` 的不掛租戶，field 為 `<根目錄檔名>:_routing_enforced[ (<租戶>)].group_by[i]`）；tenant-api 的 PUT 對 body 自己寫的值回 400 `INVALID_BODY`。不加 `--strict` 時改為 `WARN … skipping` 略過該元素後輸出（略過後為空就不輸出 `group_by`），因此 `--validate` 也會失敗；domain policy 的 `enforce_group_by` 也改判修正後的列表（元素是 mapping 時原本會當掉）。修法：加引號（`"8"`）或去掉重複／多餘的元素；`["..."]` 單獨使用仍合法。
