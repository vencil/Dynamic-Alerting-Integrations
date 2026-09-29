---
section: Fixed
topic: exporter
issues: [2100]
created: 2026-09-29T01:40:05+08:00
---
- **reload 重算 merged_hash 時讀檔失敗，之後的 tick 會重試（exporter；[#2100](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2100)）**：某次 reload 帶著租戶檔或平台檔 `tenants:` 的修改，重算時卻讀不到檔（例如 chain 上的 `_defaults.yaml` 暫時無法讀取），merged_hash 保留上一個值，但這次的輸入已經 commit。先前檔案若都沒再動，之後的 tick 偵測不到變化、不會 reload，merged_hash 就一直停在修改前的值，`/effective` 與 exporter 的 merged_hash 不一致，直到別的輸入再動為止。現在這種租戶會被標記，之後每個沒有變化的 tick 只對它重算 merged_hash 一次，直到讀檔成功；這一步不是 reload，不跑掃描與 debounce，也不動任何 reload 指標。WARN 只在失敗開始時寫一行，恢復時寫一行 INFO。只針對 reload 中的讀檔失敗：內容解析失敗重算結果不會變，所以不重試；冷啟時的合併失敗也不在此列。reload 歸因指標（`da_config_reload_trigger_total`、no-op／shadowed、blast radius）不變。
