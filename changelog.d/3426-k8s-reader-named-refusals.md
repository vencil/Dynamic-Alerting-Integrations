---
section: Changed
topic: confd-family
issues: [3426, 3472]
created: 2026-10-30T14:02:16+00:00
---
- **⚠️ Breaking：`migrate-to-operator` 遇到讀不了的來源檔改為失敗即停；k8s 讀者 lint 不再略過形狀不對的文件（[#3426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3426)）**：`--source-dir` 的檔先以 kubectl 的 manifest 讀法讀（`kind: List` 照 kubectl 展開；item 的 `kind`、`apiVersion` 都沒寫時照 kubectl 繼承 list 的），被拒收時只有純 rule file（單一 mapping 宣告 `groups`，或 YAML 讀不了但 rule loader 讀得出 `groups`）照常遷移；其餘（YAML 壞掉、頂層是 list、無 `kind`）、rule loader 拒收的 rule file、找不到讀者 binary 都結束碼 2、一個檔都不寫，逐檔點名並附讀者理由（`--json` 的 `reason` 為 `source_files_unreadable`）；先前只印 WARNING 並略過。`check_ksm_version_allowlist` 改以 k8s/ 每個檔的 kubectl 讀法判定哪裡有 KSM Deployment（不再看原文，只寫在註解裡的不算），讀不了的檔點名、結束碼 2；`lint_custom_rules`、`silencer_drift_check`、`onboard` 等判斷檔案內 rule file 的工具改用同一個判定（List 與 ConfigMap 內的 rule file 也讀到），`check_configmap_mount_completeness`、`check_scrape_reachability` 遇到讀不了的檔或形狀不對的物件改為點名拒收，不再略過；ConfigMap 的 `data` 不是 mapping、PrometheusRule 頂層寫 `groups` 或缺 `spec`（API server 會拒收）也點名，不再當成沒有 rule file。
