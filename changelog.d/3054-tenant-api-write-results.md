---
section: Fixed
topic: tenant-api
issues: [3054]
created: 2026-10-16T08:35:27+00:00
---
- **`tenant_api_writes_total` 真的開始計數，並依結果分 `result`（tenant-api；[#3054](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3054)）**：先前沒有任何程式碼遞增這支 counter，不論寫入多少次都恆為 0。現在每一次 conf.d 寫入嘗試（直接模式一次 commit、PR 模式一個分支加開 PR/MR；一個 batch 不論幾個租戶都只算一次，`?async=true` 在 task 結束時記）依結果記為 `ok`（2xx）、`conflict`（409）或 `error`（伺服器端失敗）；請求本身不合法或權限不足的不算，client 斷線後才失敗的也不算。三個 series 從啟動起就輸出（值 0）。原本不帶 label 的查詢會拿到三個 series，請改用 `sum(...)`。新增 warning 平台告警 `TenantApiWriteErrorsHigh`：1 小時內 `error` ≥ 3 筆且佔全部寫入 ≥ 50% 時觸發，同一個 Prometheus 收多個安裝時各 namespace 分開計算。
