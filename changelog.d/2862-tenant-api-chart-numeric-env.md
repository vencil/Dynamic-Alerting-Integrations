---
section: Fixed
topic: tenant-api
issues: [2862, 2927]
created: 2026-10-11T08:12:18+00:00
---
- **tenant-api chart 的數值 env 改以十進位整數輸出（helm/tenant-api；[#2862](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2862)）**：values 檔裡 1e6 以上的整數會被 helm 印成科學記號（`maxBodyBytes: 2097152` → `TA_MAX_BODY_BYTES="2.097152e+06"`），tenant-api 判為格式錯誤、印 WARN 後退回預設值，設定的值被靜默忽略。`tenantApi.server.maxBodyBytes`、`maxBatchBodyBytes`、`maxTenantDocBytes` 與 `tenantApi.writeQueueDepth` 改為先轉整數再輸出；帶小數或超出 int64 範圍的數字改為 render 失敗並指名鍵（[#2927](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2927)：原本 `1e19` 會溢位成一個與輸入無關的整數、tenant-api 照收）；字串照原樣傳給 tenant-api（格式錯誤仍由 tenant-api 印 WARN 並用預設值）。預設 render 結果不變。
