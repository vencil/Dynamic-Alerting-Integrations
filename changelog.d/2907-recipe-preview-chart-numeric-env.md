---
section: Fixed
topic: helm
issues: [2907]
created: 2026-10-11T12:26:53+00:00
---
- **recipe-preview chart 的數值 env 改以十進位整數輸出（helm；[#2907](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2907)）**：helm 把 values 檔裡的整數讀成 float64，`quote` 對 1e6 以上的值印成科學記號（`maxBodyBytes: 2097152` → `PREVIEW_MAX_BODY_BYTES="2.097152e+06"`），app.py 的 `int()` 在啟動時丟例外、容器起不來。新增 `recipe-preview.intEnv`（與 tenant-api chart 的做法相同）套到 `service.port`、`preview.maxConcurrency`、`preview.rateLimitPerMin`、`preview.maxBodyBytes`：數字先轉整數，帶小數的數字讓 render 失敗並指名鍵，字串原樣傳遞。`--set` 走整數，原本就不受影響；預設 render 不變。
