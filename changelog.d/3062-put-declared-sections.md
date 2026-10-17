---
section: Security
topic: tenant-api
issues: [3062]
created: 2026-10-17T04:10:00+00:00
---
- **⚠️ Breaking：`PUT /tenants/{id}` 只替換 body 宣告的租戶段落，同檔其他段落原樣保留（tenant-api；[#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)）**：一份 conf.d 檔可宣告多個租戶，過去只要對 `{id}` 有寫入權限，就能刪掉或改寫同檔其他租戶的段落。現在 body 宣告的每個租戶（以 `tenants:` 下解碼後的 key 判定）都要有寫入權限，否則 403、訊息不點名；PR 模式另在最新 base 上逐一重判。寫入時在寫入鎖內把宣告的段落拼進現有檔（PR 模式是最新 base 上那份），其他段落、root key 與註解保留，保留不住（例如別處 alias 指向被替換段落的 anchor）回 409 `TENANT_CONFIG_NOT_LOADABLE`（`not_round_trippable`），差異位置只給讀得到檔內所有租戶的呼叫者。語意改變：省略某段不再刪掉它；檔內還有未宣告內容時整檔以 2 格縮排重新輸出。無法拼接的檔（不能解析，或 root／`tenants:` 層有 `<<`、`tenants` 是 alias 等）一律沿用 #2405：全租戶寫入權可整檔取代，其他人 400。讀不到全檔的呼叫者，body 段落含 anchor／alias、被替換段落含 anchor（含 key），或 root／`tenants:` 節點帶 anchor，一律 409、不比較值（避免猜值 oracle）；（此規則仍透露 root／`tenants:` 是否帶 anchor 這個固定位元，刻意接受）；宣告的他租戶也被別檔宣告回 409 `TENANT_DECLARED_ELSEWHERE`。`/diff`、`/validate` 與 PUT 共用同一套拒收判定、依 PUT 的順序，PUT 會拒的回同一狀態碼與訊息（`/validate` 為 `valid: false`）；例外是不能寫 `{id}` 的讀者在 `/diff` 拿遮罩預覽，以及 PR 模式 `/validate` 只驗 body。細節見 [API 硬化 §3.1](docs/api/tenant-api-hardening.md)。
