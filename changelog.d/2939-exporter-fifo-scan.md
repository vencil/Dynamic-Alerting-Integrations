---
section: Fixed
topic: exporter
issues: [2939]
created: 2026-10-13T13:20:50+00:00
---
- **⚠️ breaking（tenant-api）：conf.d 有讀不了的項目時寫入一律拒收；設定檔名的 FIFO 不再讓掃描卡住（exporter、da-guard、tenant-api；[#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939)）**：FIFO、socket、裝置檔（或指向它們的 symlink）在所有掃描與重讀一律不開不讀，記為讀不到（`reason` 新增 `special_file`：`da_config_unreadable_files`、`ConfigFilesUnreadable`、`da-guard` 的 exit 3；`_defaults` 檔另計入 `da_config_defaults_unusable{reason="unreadable"}`），其他租戶照常 reload；過去 reload、`da-guard` 與 tenant-api 的 `/effective` 會卡住。tenant-api 判寫入後的整棵樹（ADR-036 §4）：樹上有讀不了的項目（`special_file`、懸空 symlink 等 `stat_error`、`read_error`、`walk_error`）而不是本次要寫的租戶檔時，`PUT`／custom-alerts／`/diff` 回 409、批次回 422，`code: TENANT_TREE_UNREADABLE`，`/validate` 回 `valid: false`。原本回 500 的寫入（別處有 FIFO）改回 409／422，原本照常寫入的（懸空 symlink 等）也改為拒收；要寫的租戶檔本身是懸空 symlink 時照常寫入，是修復路徑。只有對所有租戶有寫入權限的呼叫者看得到項目名稱。
