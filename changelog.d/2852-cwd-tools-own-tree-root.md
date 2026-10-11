---
section: Changed
topic: dx
issues: [2852]
created: 2026-10-11T09:14:18+00:00
---
- **`bump_playbook_versions`、`check_devrules_size`、`check_path_metadata_consistency`、`check_playbook_freshness` 改為作用在工具自己所在的那棵樹，不再看 cwd（[#2852](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2852)）**：原本從目前目錄往上找第一個 `.git` 當 repo 根目錄，所以在另一個 checkout 裡執行時，會讀那個 checkout 的檔案，`bump_playbook_versions` 還會寫進去。現在一律取包住工具的 `scripts/tools` 所在目錄，不在任何 `scripts/tools` 底下時回 rc 2。要處理另一個 checkout，請執行那個 checkout 裡的工具。`check_path_metadata_consistency --config-dir` 指定的掃描目錄不受影響；但掃描目錄在本樹之外時，輸出的檔案路徑改為絕對路徑（原本相對於 cwd 所在的 repo）。
