---
section: Fixed
topic: portal
issues: [2406]
created: 2026-09-29T14:02:37+00:00
---
- **Tenant Manager 的自訂告警視窗會說明租戶檔無法載入，不再誤報為「被別人改過」（portal；[#2406](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2406)）**：`GET /api/v1/tenants/{id}` 帶 `config_error` 時（[#2373](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2373)），視窗不開放新增、編輯或儲存，改顯示原因（中英雙語，附原始代碼），因為後端會拒絕對這種檔的部分寫入。儲存收到 409 `TENANT_CONFIG_NOT_LOADABLE` 時，顯示後端回的原因並保留編輯內容與複製備份按鈕，不再顯示「遠端設定已被其他人更新，請重整重試」——那個訊息只屬於 `base_hash` 衝突，重整重試對壞掉的租戶檔永遠不會成功。
