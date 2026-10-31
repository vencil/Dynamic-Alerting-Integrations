---
section: Changed
topic: confd-family
issues: [3438]
created: 2026-10-31T08:54:11+00:00
---
- **⚠️ Breaking：repo 出貨的 rule 改由路徑宣告，Helm values 注入的 rule 須登記（[#3438](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3438)）**：`_lib_consumer_read.RULE_SOURCES` 宣告 `rule-packs/`、`operator-manifests/`、`k8s/03-monitoring/` 三處的 rule 檔與旁邊的其他 YAML，`rule_pack_files` 即其中一筆；repo 的 rule 契約只讀宣告的檔（經 `rule_files_in`），任一處配不到檔即失敗。chart values 以 `{{ toYaml .Values.x }}` 注入的 rule 須登記在 `CHART_VALUES_RULES`（值為 rule file 或 rule list）才納入契約；宣告以外長得像 rule 的 YAML 由覆蓋率絆線點名，要求宣告或以 `NOT_RULE_FILES` 附理由列為非 rule。另：`da-crdecode` 的 manifest 讀法拒收「`items` 旁邊還有自己內容」的 list 文件（kubectl 會不出聲丟掉那些內容），並點名那些鍵；ConfigMap 同一個 key 同時出現在 `data` 與 `binaryData` 時以 API server 的理由（`data[<key>]: … duplicate of key present in binaryData`）點名拒收。
