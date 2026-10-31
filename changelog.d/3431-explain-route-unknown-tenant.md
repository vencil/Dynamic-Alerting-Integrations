---
section: Changed
topic: da-tools
issues: [3431]
created: 2026-10-31T02:27:59+00:00
---
- **explain-route 的 `--tenant` 指到樹裡沒宣告的租戶時結束碼 2（[#3431](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3431)）**：一般模式與 `--trace` 都在產出任何結果前以 `reason: tenant_not_found` 拒絕，逐一點名每個不存在的租戶，與 diagnose、batch-diagnose、explain-keys 對同一件事的回答一致。**Breaking**：過去只印 `WARN: tenant '…' not found`、輸出空清單（或略過該租戶）並以 0 結束。有宣告（`tenants:` 底下有這個 key，值是什麼都算）但產生器不為它產出路由的租戶（id 不合法、內容是 null）不算不存在，仍略過，stderr 改說明產生器不為它產出路由，不再說「not found」。
