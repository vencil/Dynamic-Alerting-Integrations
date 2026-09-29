---
section: Fixed
topic: docs
issues: [2441]
created: 2026-09-29T13:40:00+00:00
---
- **「會被擋」類產品行為宣稱對齊程式（docs；[#2441](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2441)）**：`tenant-api-hardening`（中英）的限流寫明是依呼叫者（`X-Forwarded-Email`，沒有則來源 IP）在任一 60 秒內計數、只有 `/health`／`/ready`／`/metrics` 免計；`for-tenants`（中英）沒有 base 的 `<base>_critical` 被擋時實際回 `400` 與 `has no base metric ... in defaults`（不是 unknown key），直推 GitOps 時 exporter log 會印同一句 WARN。其餘六行逐行對過程式，維持不變。
