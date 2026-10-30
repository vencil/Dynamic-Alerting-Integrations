---
section: Changed
topic: confd-family
issues: [3426]
created: 2026-10-30T14:02:16+00:00
---
- **⚠️ Breaking：`migrate-to-operator` 遇到讀不了的來源檔改為失敗即停；k8s 讀者 lint 不再略過形狀不對的文件（[#3426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3426)）**：`--source-dir` 的檔先以 kubectl 的 manifest 讀法讀，被拒收時只有「單一 mapping、宣告 `groups`」的純 rule file 照常遷移；其餘（YAML 壞掉、頂層是 list、無 `kind`、`kind: List`，例如 `kubectl get cm -o yaml` 的輸出）結束碼 2、一個檔都不寫，逐檔點名並附讀者理由（`--json` 的 `reason` 為 `source_files_unreadable`）；先前只印 WARNING 並略過。`check_ksm_version_allowlist` 改以 k8s/ 每個檔的 kubectl 讀法判定哪裡有 KSM Deployment（不再看原文，只寫在註解裡的不算），讀不了的檔點名、結束碼 2；`check_configmap_mount_completeness` 與讀 ConfigMap／PrometheusRule 內 rule file 的工具遇到非物件文件改為點名拒收，不再略過。
