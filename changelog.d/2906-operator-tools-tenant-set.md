---
section: Changed
topic: confd-family
issues: [2906]
created: 2026-10-20T14:14:16+00:00
---
- **⚠️ Breaking：`operator-generate` 與 `migrate-to-operator` 的租戶改由 parse-tree 認定，不再取檔名（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：AlertmanagerConfig 依整棵 conf.d（含子目錄）各租戶檔 `tenants:` 的鍵產出，鍵依原文讀（`010:` 是租戶 `010`），與 exporter 服務的租戶一致。先前取根目錄檔名主幹：`team-a.yaml` 裡宣告 `db-a` 卻產出 `da-tenant-team-a`，子目錄的租戶沒有 CRD，沒有 `tenants:` 的平面格式檔反而有。現在 parse-tree 讀不了的租戶檔（含名為 `*.yaml` 的目錄、懸空 symlink；逐一指名檔與 kind）、同一租戶在兩個檔宣告、或沒有可用的 da-guard，都以 exit `2` 結束，不產出任何 CRD（兩支的 `--json` envelope `reason` 皆為 `tenant_set_unknown`）。不符合 RFC 1123 的租戶 id 照舊警告後略過。
