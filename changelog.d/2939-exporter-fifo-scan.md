---
section: Fixed
topic: exporter
issues: [2939]
created: 2026-10-13T13:20:50+00:00
---
- **conf.d 裡設定檔名的 FIFO 不再讓掃描永遠卡住（exporter、da-guard、tenant-api；[#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939)）**：不是一般檔的項目（FIFO、socket、裝置檔，或指向它們的 symlink）過去只有 `da-guard parse-tree` 略過，exporter 的掃描照常去讀，讀 FIFO 會一直等不到資料：reload 停住、`da-guard` 各子命令與 tenant-api 的 `/effective` 都不會結束。現在所有掃描與重讀都用同一個判斷，一律不開不讀，記為讀不到（`reason` 新增 `special_file`：`da_config_unreadable_files`、告警 `ConfigFilesUnreadable`、`da-guard` 的 `unreadable` 與 exit 3；`_defaults` 檔另計入 `da_config_defaults_unusable{reason="unreadable"}`），其他租戶照常 reload。
