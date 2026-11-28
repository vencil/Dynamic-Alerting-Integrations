---
section: Changed
topic: exporter
issues: [4263, 4254]
created: 2026-11-24T15:57:23+00:00
---
- **⚠️ breaking：閾值過了 `expires:`，改送刪掉那一行時租戶會拿到的值（exporter、da-guard、tenant-api；[#4263](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4263)、[#4254](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4254)）**：先前一律退回根 `_defaults.yaml` 的值，跳過子目錄 `_defaults.yaml`、profile 與平台檔 `tenants:` 給的值；現在依序退回下面一層，都沒有才是根目錄的值。寫在平台檔或 profile 裡的 `expires:` 同樣適用。`<鍵>_critical` 與維度鍵的 `expires:` 也生效了（先前一律不到期），下面一層或根目錄給了數字就退回它。下面沒有任何會送出的值時（例如維度鍵下面那層寫的不是數字），照舊在過期後照送原值、不會變靜默；tenant-api 寫入時擋下這種 `expires:`，訊息說明沒有可退回的值，已在磁碟上的由 exporter 每次載入時記一行 WARN。以 `_` 開頭的保留鍵不是閾值，`expires:` 對它們不生效。到期事件 `threshold_expired` 跟著只為真的會退回的值發出。exporter 到了樹裡下一個 `expires:` 的時刻，即使檔案沒變也會重新合併；新指標 `da_config_valid_until_unixtime_seconds` 是那一刻，重新合併超過 5 分鐘沒提交時告警 `ConfigExpiryRebuildOverdue`（平台規則包）；`da-guard` 帶 `--at` 的指令改在那一刻合併整棵樹。
