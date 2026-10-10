---
section: Changed
topic: confd-family
issues: [1549]
created: 2026-10-10T02:45:29+00:00
---
- **⚠️ breaking：`describe_tenant` 與 `tenant-verify` 的 `merged_hash` 改用 da-guard 的值，純搬檔不再算爆炸半徑（tools；[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)）**：`--show-sources` / `--all` 與 `tenant-verify` 的 `merged_hash` 讀自 `da-guard effective`，與 tenant-api `/effective` 相同（`--what-if` 的兩個 hash 仍是自算）。da-guard 給不出值時 describe 印 `null` 並附原因（`merged_hash_error`）。`tenant-verify` 的變化：升級後 `tenant-verify` 的快照全部重拍；沒有 da-guard、或樹中有 exporter 讀不了的檔時回 1；有重複宣告的樹，其餘租戶也回 1（da-guard 整棵拒收）。`blast_radius` 以 `merged_hash` 判斷值是否變更，自訂告警另比 recipe 內容、名稱與是否為租戶自己的，不比宣告所在的檔：只搬檔時 `affected_tenants` 為 0。base 與 PR 兩份 `describe-tenant --all` 輸出要用同一版重產，混用舊輸出會多出 Tier B 的 `merged_hash` 條目。
