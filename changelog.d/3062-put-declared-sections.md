---
section: Security
topic: tenant-api
issues: [3062, 3064]
created: 2026-10-17T04:10:00+00:00
---
- **⚠️ Breaking：租戶分為 API 管理與 git 管理，共用檔的租戶不再能經 API 寫入（tenant-api；[#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)、[#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064)）**：過去對 `{id}` 有寫入權就能刪改同檔其他租戶的段落。現在只有檔案「API 形狀」的租戶可經 API 寫入：一份 document、只宣告這個租戶且 id 等於檔名、頂層與 `tenants:` 底下沒有 anchor／alias／merge key／帶 tag 的 key。其他形狀（多租戶共用檔、不是 YAML 等）改由 git 管理：`PUT`、custom-alerts、批次與群組批次（兩種寫入模式）一律回 409 `TENANT_GIT_MANAGED`，`/diff` 同，`/validate` 回 `valid: false` 加 `code`。`PUT` 的 body 也必須只宣告 `{id}`。API 管理的租戶，`X-DA-Base-Hash` 與 custom-alerts 的 `base_hash` 照舊。細節見 [API 硬化 §3.1](docs/api/tenant-api-hardening.md)。
