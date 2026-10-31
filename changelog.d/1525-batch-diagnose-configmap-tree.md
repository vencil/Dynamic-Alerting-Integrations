---
section: Changed
topic: da-tools
issues: [1525]
created: 2026-10-30T16:26:01+00:00
---
- **batch-diagnose 的租戶與繼承鏈改由 exporter 對 ConfigMap 的讀法回答（[#1525](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1525)）**：把 `threshold-config`（`data` 與 `binaryData`；後者照 API server 的讀法解碼，折行的 base64 也收、混入其他字元拒收）落成暫存樹，以 da-guard 讀一次：租戶是檔內 `tenants:` 宣告的 id，不再以 ConfigMap key 的檔名推（一個檔宣告多個租戶時，過去回報的是檔名、真租戶漏掉）；每個租戶的結果附 `inheritance_chain`，過去 batch 從不查 conf.d。**Breaking**：`--tenants` 模式也讀 ConfigMap，指名的租戶不在其中時結束碼 2（`tenant_not_found`）；exporter 讀不了的樹（壞檔、同一租戶宣告在兩個檔）整批結束碼 2、不檢查任何租戶，過去同 stem 兩個檔是「算一次」照跑；找不到 da-guard 時結束碼 2，過去是「No tenants found」。`diagnose.check()` 新增 `chain=` 參數。
