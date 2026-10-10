---
section: Security
topic: tenant-api
issues: [1560]
created: 2026-10-10T01:12:26+00:00
---
- **receiver 憑證只給有寫入權限的人看（tenant-api；[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560) 選項 d）**：行為變更。原本 `GET /tenants/{id}`、`/effective`、`POST /tenants/{id}/diff` 把 receiver 憑證（webhook／Slack／Teams／Rocket.Chat URL、PagerDuty key、密碼、token）原樣回給任何讀得到該租戶的人；現在只有對該租戶有寫入權限的呼叫者（與 `PUT` 同一個判定）看到原值，其他人看到 `<masked: write permission required>` 並帶 `masked: true`。遮罩後的 `raw_yaml` 重新序列化且不含註解；檔案用了 anchor／alias／merge key、多個 document 或不是 YAML 時，`GET` 改回空的 `raw_yaml`、`custom_alerts` 加 `raw_yaml_withheld: true`，`/diff` 回 422 `MASKED_PREVIEW_UNAVAILABLE`。`/diff` 對被遮罩的人改為兩側都遮罩後再比對，不再能用「猜對回空 diff」逐個猜憑證。`PUT`／`/validate` 的 body 含這個佔位字串回 400。open mode 不授予寫入權限，所以所有人都看到遮罩版。另外 `/diff` 的標頭改為 `current/<id>.yaml`／`proposed/<id>.yaml`，不再含 conf.d 與暫存檔的絕對路徑。
