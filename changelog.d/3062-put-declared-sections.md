---
section: Security
topic: tenant-api
issues: [3062]
created: 2026-10-17T04:10:00+00:00
---
- **⚠️ Breaking：`PUT /tenants/{id}` 只替換 body 宣告的租戶段落，同檔其他段落原樣保留（tenant-api；[#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)）**：一份 conf.d 檔可宣告多個租戶，過去只要對 `{id}` 有寫入權限，就能刪掉或改寫同檔其他租戶的段落。現在 body 宣告的每個租戶（以 `tenants:` 下解碼後的 key 判定）都要有寫入權限，否則 403、訊息不點名；寫入時在寫入鎖內把宣告的段落拼進現有檔（PR 模式是最新 base 上那份），其他段落、root key 與註解保留，保留不住（例如別處 alias 指向被替換段落的 anchor、`tenants:` 層有 `<<`）回 409 `TENANT_CONFIG_NOT_LOADABLE`（`not_round_trippable`）。語意改變：省略某段不再刪掉它；檔內還有未宣告內容時整檔以 2 格縮排重新輸出。讀不出租戶的檔沿用 #2405。細節見 [API 硬化 §3.1](docs/api/tenant-api-hardening.md)。
