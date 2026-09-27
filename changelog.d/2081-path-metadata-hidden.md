---
section: Fixed
topic: confd-family
issues: [2081]
created: 2026-09-27T07:34:31+08:00
---
- **`check_path_metadata_consistency` 不再掃描 exporter 看不到的隱藏路徑（lint；[#2081](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2081)）**：租戶檔選取與「not checked」報告改走共用的 `list_config_tree` 一次走訪，與 exporter 一樣略過 `.` 開頭的檔案與目錄、不跟進目錄 symlink。先前 `.old.yaml`、`.snap/` 下的檔案會被報 mismatch，而 ConfigMap 掛載版面的 `..<timestamp>/` payload 讓同一個租戶檔被算兩次。
  另一個行為改變：讀不到的目錄（含 conf.d 根本身；例如非 root 身分下權限不足）先前被靜默丟棄，現在會以 `not checked` 警告列出，`_` 開頭的目錄也一樣；工具仍一律 exit 0。
