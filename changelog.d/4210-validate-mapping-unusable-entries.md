---
section: Fixed
topic: tools
issues: [4210]
created: 2026-11-22T11:07:47+00:00
---
- **`da-tools validate --mapping` 不再靜默略過不能用的項目（tools；[#4210](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4210)）**：prefix-mapping.yaml 裡值留空、`old_query`／`new_query` 只寫一個、欄位不是字串、或什麼都沒寫的項目，先前會被跳過，剩下的組照常比對，看起來像整份都驗過。現在整次執行以結束碼 2 停下，stderr 一行點名每個項目的 key 與原因。migrate 正常產出的檔不受影響。
