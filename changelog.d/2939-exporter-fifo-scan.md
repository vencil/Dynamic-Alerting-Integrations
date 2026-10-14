---
section: Fixed
topic: exporter
issues: [2939]
created: 2026-10-13T13:20:50+00:00
---
- **⚠️ breaking（tenant-api）：conf.d 有讀不了的項目時寫入一律拒收；設定檔名的 FIFO 不再讓掃描卡住（exporter、da-guard、tenant-api；[#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939)）**：FIFO、socket、裝置檔（或指向它們的 symlink）在所有掃描一律不開不讀，記為讀不到（`reason` 新增 `special_file`：`da_config_unreadable_files`、`ConfigFilesUnreadable`、`da-guard` exit 3；`_defaults` 另計入 `da_config_defaults_unusable{reason="unreadable"}`）；過去 reload、`da-guard` 與 `/effective` 會卡住。tenant-api 判寫入後的整棵樹（ADR-036 §4）：別處有無法解析的 symlink（懸空、迴圈、ENOTDIR）回 409 `TENANT_TREE_UNREADABLE`（在 git 修）；別處有伺服器讀不了的項目（`special_file`、`read_error`、`walk_error`）回 500 `TENANT_STORE_FAULT`（在伺服器修，批次整批 500；新 gauge `tenant_api_store_unreadable_entries`（scrape 時自掃）、告警 `TenantApiStoreUnreadable`；量不到時 gauge 不輸出、由 `TenantApiStoreScanStale` 告警）。原本回 500 或照常寫入的都改為拒收。要寫的租戶檔是這種 symlink 時照常寫入；讀不了或是 FIFO 時一律 409 `TENANT_CONFIG_NOT_LOADABLE`。PR 模式寫入改與直寫一致：指向 repo 內另一檔的 symlink 會換成一般檔（git 記為刪 120000、增 100644），不再穿透寫到目標。只有全域寫入者看得到項目名稱。
