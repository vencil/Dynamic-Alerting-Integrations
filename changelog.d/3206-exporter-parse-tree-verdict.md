---
section: Changed
topic: exporter
issues: [3206]
created: 2026-10-21T01:34:42+00:00
---
- **⚠️ breaking：exporter 不服務 `da-guard parse-tree` 判為讀不了的 conf.d 檔（exporter、tenant-api 讀取面、da-guard；[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §2）**：今天照讀、之後整份不服務的寫法：一個檔裡有多份文件、只有 `---`、註解或 `~` 的尾端文件、`!!binary`、`!!set`、非特定標記 `!`、檔案中段的 BOM、null 鍵（`~:`）；本地標記（`!foo`）與 UTF-16 檔同樣。租戶檔：檔內租戶不服務；`_` 平台檔（含子目錄的）：整份略過，defaults 鏈當空檔。錯誤文字改為 parse-tree 的說明（超過 512 bytes 的保留頭尾、中間省略），log 帶 `kind`，新 counter `da_config_parse_refused_total{file_basename,kind}`。tenant-api：租戶列表該檔 `config_error` 為 `invalid_config`，批次 patch 回 `base_refused`，`/effective` 不再因這類檔回 500。da-guard 對這些檔回 exit 3；未判定證據 `generator_unreadable` 改為 `unreadable_outside_run`，只列 `--scope` 外與像設定檔的目錄。升級前：在 CI 以新版 da-guard 驗 conf.d，回 exit 3 的檔就是新版 exporter 不服務的檔。
