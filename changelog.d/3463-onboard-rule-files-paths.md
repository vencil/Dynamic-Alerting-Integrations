---
section: Changed
topic: confd-family
issues: [3463]
created: 2026-11-07T00:22:35+00:00
---
- **⚠️ Breaking：`onboard --rule-files` 只吃路徑、不再展開 glob（[#3463](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3463)）**：改為收一個或多個檔或目錄（目錄遞迴取 `*.yaml`／`*.yml`），與 `lint`、`silencer-drift-check` 相同。存在的 `rules/b[1].yaml` 由結束碼 2（被當成 glob、選不到）改為照讀、0；未加引號、由 shell 展開成多個檔由 2（`unrecognized arguments`）改為 0；加了引號的 `'rules/*.yaml'` 由 0 改為 2，stderr 點名旗標與路徑。可重複給，值會累加。Kubernetes Job 的 `args` 不經過 shell，請改給目錄（v2.9.0 映像給目錄選不到檔，升級前沿用加引號的 glob）。給的目錄裡沒有規則檔時的訊息改為與 `lint --ci` 同一句 `selects nothing (…)`。
