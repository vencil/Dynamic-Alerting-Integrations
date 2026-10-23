---
section: Added
topic: da-tools
issues: [3229]
created: 2026-10-23T12:00:00+00:00
---
- **`da-guard served-values` 與 `da-guard effective` 新增 `--tenant`：整棵樹照讀，只算指定的租戶（da-tools；ADR-038 §5）**：可重複；平台檔、`parse_failed`／`unreadable`／`skipped` 與重複宣告的拒收都與全樹相同，每個要求的租戶得到的條目與全樹輸出相同。文件另帶 `tenants_requested` 與 `tenants_absent`（樹裡沒有的租戶，不因此回 2）。`/metrics` 能否 Gather 只就要求的租戶判。Python 的 `load_served_tree`／`load_effective_tree`／共用判決 `judge_tree` 新增 `tenants=`，判決只判那些租戶；profile 鍵的 finding 不屬於單一租戶，同樣只就要求的租戶判。`tenants=` 給裸字串、非字串或非 UTF-8 的 id 一律拒收，不會被拆成字元。5000 個租戶的樹上（單核）：served-values 由約 8.1 秒／358 MiB 降為約 2.0 秒／64 MiB，effective 由約 8.1 秒／249 MiB 降為約 2.6 秒／116 MiB。
