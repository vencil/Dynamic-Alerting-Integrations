---
section: Changed
topic: da-tools
issues: [1424]
created: 2026-09-28T12:00:00+00:00
---
- **`da-tools init` 產生的 `.da-init.yaml` 不再寫固定的 `version: 2.2.0`（[#1424](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1424)）**：那個欄位是寫死的舊版號，沒有任何程式讀它，只會讓人誤判 repo 是哪一版產生的。檔頭也改成說明它實際的作用：這個檔存在時，再跑一次 `init` 會被擋下，除非加 `--force`（會覆寫全部檔案）。原本「供升級偵測用」的說法已移除——目前沒有升級偵測功能。
