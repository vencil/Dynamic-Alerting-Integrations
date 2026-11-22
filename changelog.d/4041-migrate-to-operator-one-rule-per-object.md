---
section: Fixed
topic: da-tools
issues: [4041]
created: 2026-11-22T01:30:00+00:00
---
- **⚠️ `migrate-to-operator` 不再以同名檔互相覆寫，物件名稱與 `operator-generate` 對齊（[#4041](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4041)）**：一個 ConfigMap 有多個 rule 檔 key 時，舊版每個 key 各產生一個同名的 PrometheusRule，後者蓋掉前者，結束碼仍是 `0`、總結照報兩個；本平台規則包的 ConfigMap 多半是兩個 key，轉出來少了 recording rule。現在同一個來源物件合併成一個 PrometheusRule。兩個來源產出同名物件、名稱不是合法的 Kubernetes 名稱、與來源裡既有的 PrometheusRule 同名、或同一個 ConfigMap 的兩個 key 有同名 group，都是結束碼 `2` 且不寫檔。**Breaking**：`prometheus-rules-<pack>` 的 ConfigMap 現在產出 `da-rule-pack-<pack>`（舊版是 `da-rule-pack-prometheus-rules-<pack>`）；套用過舊版輸出的叢集要刪除舊名物件，checklist 的 Phase 5 有指令。`configmap_files` 改為數來源物件。
