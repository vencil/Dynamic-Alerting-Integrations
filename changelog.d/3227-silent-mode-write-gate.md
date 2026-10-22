---
section: Changed
topic: tenant-api
issues: [3227]
created: 2026-10-22T01:13:06+00:00
---
- **`_silent_mode` 的值改在寫入關卡以 exporter 的讀取函式判定（tenant-api；[#3227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3227)）**：PUT、batch 與 dry-run 共用同一判定，handler 不再自抄詞表。`off`／`disabled`／`false` 與前後空白、大小寫不同的寫法都合法。
  ⚠️ 破壞性：exporter 會忽略的值（`purple`）在 PUT 回 400；batch 由整批 400 `INVALID_BODY` 改為該筆 op 失敗（422，op code `BAD_REQUEST`），async 改為任務失敗。檔裡既有的錯值若本次寫入沒改到，降為 notice、不擋寫入（GET 亦列為 notice）。登錄表沒有的 `_` 鍵（如 `_timeout_ms`）維持 400、理由改寫；`_metadata`／`_custom_alerts`／`_namespaces` 不能用 batch 字串 patch；`_profile`／`_routing_profile` 不再有 256 字上限。
