---
section: Fixed
topic: confd-family
issues: [2127]
created: 2026-09-28T08:06:09+00:00
---
- **`check_path_metadata_consistency` 的 mismatch 顯示被掃描的那個路徑，不再顯示 symlink 目標（lint；[#2127](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2127)）**：判定本來就用原始路徑、結論正確，只有顯示時先 `resolve()` 跟進了檔案本身的 symlink。ConfigMap 掛載版面（`staging/acme.yaml -> ..data/acme.yaml`）因此被顯示成 `..<timestamp>/` payload 內的檔案；`prod/acme.yaml -> ../staging/acme.yaml` 則被顯示成本身一致的 `staging/acme.yaml`，指向不該改的檔。現在 `--ci` 與一般輸出都顯示原始路徑（相對 repo 根目錄），`--config-dir` 或 repo 根目錄經 symlink 傳入時仍為相對路徑（`conf.d` 本身指到 repo 外時照舊顯示絕對路徑）。
