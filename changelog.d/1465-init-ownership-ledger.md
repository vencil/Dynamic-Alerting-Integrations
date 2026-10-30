---
section: Changed
topic: da-tools
issues: [1465]
created: 2026-10-30T12:35:59+00:00
---
- **`da-tools init` 不再覆寫不屬於它的檔：`.da-init.yaml` 改記檔案清冊（[#1465](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1465)）**：⚠️ 行為變更。`.da-init.yaml` 新增 `files:`，記下 init 產生的每個檔與其 sha256，是「哪些檔屬於 init」的唯一依據。沒有 `.da-init.yaml` 時 init 不擁有任何檔：要寫的路徑上已有檔（例如手寫的 `conf.d/_defaults.yaml`）就拒絕、rc 1、不寫入任何檔，要覆寫須加 `--force`；以前這種情況 rc 0、直接蓋掉手調的閾值。`--force` 會在摘要點名 init 上次產生後被改過的檔；`--dry-run` 不需 `--force`、已初始化的目錄也能跑，並標出每個既有檔的歸屬與是否改過。舊版產生的 `.da-init.yaml` 沒有清冊，改過與否會標為無法判斷，下一次執行後補上。
