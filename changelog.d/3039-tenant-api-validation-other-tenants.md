---
section: Security
topic: tenant-api
issues: [3039, 1530]
created: 2026-10-16T00:20:49+00:00
---
- **驗證訊息不再寫出 caller 讀不到的租戶（tenant-api；[#3039](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3039)、[#1530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1530)）**：一個檔宣告多個租戶時，key 驗證會判整個檔，訊息原本會寫出同檔其他租戶的名稱與 key 名（例如 `tenant=t-b: unknown routing key …`）。現在 PUT、batch PATCH（同步回應與 PR 模式）、custom alerts、`POST /tenants/{id}/validate` 的 400／422 文字與 `GET /tenants/{id}` 的 `validation_warnings`／`validation_notices`，只寫出 caller 讀得到的租戶；其他租戶的訊息只剩一行「another place in the file has problems／notices」，細節進 server log。非同步 batch 存下的結果一律不寫出其他租戶。部分寫入拒收時點名的位置，改為 caller 讀得到的租戶區塊也寫全路徑。擋不擋寫入不變。
