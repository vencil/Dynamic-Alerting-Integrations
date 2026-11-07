---
section: Changed
topic: tenant-api
issues: [3504]
created: 2026-11-07T00:14:11+00:00
---
- **⚠️ BREAKING：租戶只從 `tenants.<id>` 讀，tenant-api 不再把平鋪檔解成租戶（tenant-api／exporter；[#3504](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3504)）**：先前 GET 與 `/effective` 遇到沒有宣告被請求租戶的檔，會把整份檔改讀成平鋪的鍵值、掛到該租戶名下，exporter 卻從不讀這種檔；宣告別的租戶的檔因此在請求的 id 下出現一個叫 `tenants` 的鍵，還多出一列 `/metrics` 不會送的閾值。現在這類檔不給被請求的租戶任何內容，與 `/metrics` 一致。頂層有 `tenants` 以外 key 的檔（例如沒有 `tenants:` 包裝、直接平鋪閾值的檔）改判為 git 管理（`git_managed_reason: not_tenant_document`），GET 不回原文、所有寫入回 409；tenant-config schema 早已不允許這種 key。空檔、只有註解或 `tenants: {}` 的空殼檔照舊是 API 管理。遷移：把平鋪的鍵移到 `tenants:\n  <id>:` 底下。
