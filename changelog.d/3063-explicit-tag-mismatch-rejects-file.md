---
section: Changed
topic: exporter
issues: [3063]
created: 2026-10-17T06:26:37+00:00
---
- **⚠️ breaking：租戶檔裡顯式 tag 與內容不符的純量（如 `mysql_connections: !!int abc`）改為整份拒收（exporter、tenant-api、da-guard；[#3063](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3063)）**：過去 exporter 照常服務這種檔、該閾值退回預設值，`/effective`、merged_hash 與路由產生器卻拒收，tenant-api 的 `GET /api/v1/tenants/{id}` 回 500、列表與 search 判為健康，同檔他租戶的壞值也會讓無辜租戶 500。現在租戶檔的判定與泛型解碼一致：整份跳過（`WARN: skip unparseable file`、`da_config_parse_failure_total` 上升），同檔所有租戶從 `/metrics` 消失；tenant-api 單筆、列表、search 一律回 200 `config_error: invalid_config`（不帶衍生欄位），`/effective` 回 404。breaking 的理由：原本部分服務的檔改為整份不服務。tag 與內容相符（`!!int 5`、`!!str 5`、`!!float 1.5`、`!!bool true`）不受影響。修法：去掉 tag 或改成相符的值；以 API 修需整檔 `PUT`，現有檔解析不了，故需對所有租戶都有寫入權限（#2405），否則直接改 git。`_` 平台檔維持原判定。
