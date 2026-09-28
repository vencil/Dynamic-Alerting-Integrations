---
section: Fixed
topic: confd-family
issues: [2360]
created: 2026-09-28T16:09:39+00:00
---
- **`check_confd_schema` 與 `check_retire_drift` 不再讀 exporter 看不到的隱藏路徑（lint；[#2360](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2360)）**：與 exporter 的走訪一致，`.` 開頭的檔案與目錄一律靜默略過。先前 `check_confd_schema --config-dir` 會進入 `.snap/` 這類隱藏目錄，其中一個壞掉的 YAML 就讓整次檢查 exit 2；`check_retire_drift` 則把 `.ghost.yaml`、`.snap/` 下檔案裡的租戶當成已宣告租戶，要求它們有 K8s target。兩支現在都列入 `tests/shared/test_confd_hidden_axis_across_tools.py` 的正式檢查格。
