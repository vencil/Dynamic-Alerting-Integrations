---
section: Fixed
topic: exporter
issues: [2100]
created: 2026-09-29T01:40:05+08:00
---
- **reload 重算 merged_hash 時讀檔失敗，下一個 tick 會重試（exporter；[#2100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2100)）**：某個 tick 帶著租戶檔或平台檔 `tenants:` 的修改，重算時卻讀不到檔（例如 chain 上的 `_defaults.yaml` 暫時無法讀取），merged_hash 會保留上一個值，但這一輪的輸入已經 commit；先前之後的 tick 若檔案都沒再動，就一直沿用舊值，`/effective` 與 exporter 的 merged_hash 不一致，直到別的輸入再動為止。現在這種租戶會被標記，之後每個 tick 重算一次直到讀檔成功；WARN 只在失敗開始時寫一行，恢復時寫一行 INFO。只針對讀檔失敗：解析失敗的內容重算結果不會變，所以不重試。reload 歸因指標（`da_config_reload_trigger_total`、no-op／shadowed、blast radius）不變：補算的那個 tick 不計為一次 reload。
