---
section: Changed
topic: da-tools
issues: [3512]
created: 2026-11-01T23:37:41+00:00
---
- **offboard-tenant 的 `--state` 拒絕訊息改為中文（da-tools；[#3512](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3512)）**：`_lib_tenant_state` 的訊息集中為 en／zh 語言表，`load_state` 的 `lang` 必填、無預設，由呼叫端傳入；offboard-tenant 傳 `zh`，原本「英文訊息接中文提示」的同一行不再中英混出。generate-tenant-metadata 維持英文、字串不變。結束碼不變。
