---
section: Changed
topic: tools
issues: [4040]
created: 2026-11-17T23:48:01+00:00
---
- **`rule-pack-diff --from`／`--to` 與 `migrate` 的規則檔改為宣告的輸入（tools；[#4040](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4040)）**：每個檔只讀一次，Prometheus 的 rule loader 讀的是那份內容，判定後不再開檔。不存在、讀不了、不是 UTF-8 的檔，結束碼 `2`、一行點名旗標與路徑（`migrate` 的位置參數只點名路徑），`--from` 與 `--to` 都有問題時一次列出；先前訊息只點名路徑與讀者。這三個輸入因此也進了故障注入閘門。
