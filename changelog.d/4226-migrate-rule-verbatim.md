---
section: Changed
topic: da-tools
issues: [4226, 4229]
created: 2026-11-28T12:00:00+00:00
---
- **⚠️ Breaking：`da-tools migrate` 改為原樣遷移一個租戶的規則（da-tools；[#4226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4226)）**：用法 `migrate <rules.yml> --tenant X --owner O --expiry YYYY-MM-DD -o <草稿目錄>`。每條規則的 expr 語意、`for`、labels、annotations、alertname 不變，只以 Prometheus 的語法樹在每個 selector 注入租戶隔離（帶 tenant 的指標 `tenant="X"`，kube-state-metrics 有 namespace 的物件與 cAdvisor／kubelet 的基礎設施指標 `namespace="X", tenant=""`），加 `tenant`／`component: migrated` label 與 `owner`／`expiry` annotation，附到期前 14 天起響的伴隨告警；同檔被讀到的 recording rule 一起遷移並改名 `migrated:<租戶>:<原名>`，與 legacy 規則載入同一台也不互讀。注入選不到單一租戶的規則（叢集層級物件、平台自己的序列、`up`）、與注入衝突的 matcher、保留 `component` 的運算式、讀到檔裡沒有的 record，逐條點名、rc 2、不寫檔；寫之前先對輸出跑 `lint`。產物 `.migration/migrated/X/rules.yml` 由抓取原始指標的 Prometheus（federation 的 edge）載入；`.migration/mapping/X.yaml` 記錄每個 selector 注入前後，報告 `migration-report-X.txt`。移除 `custom_` 閾值 key、租戶檔與 `_defaults` 片段、平台 recording／alert rule、`prefix-mapping.yaml`、triage CSV，以及 `--interactive`、`--triage`、`--prefix`、`--no-prefix`。
