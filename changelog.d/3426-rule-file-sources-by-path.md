---
section: Changed
topic: confd-family
issues: [3426]
created: 2026-10-31T01:30:00+00:00
---
- **⚠️ Breaking：rule file 工具讀哪些檔改由路徑宣告（[#3426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3426)）**：`lint`、`silencer-drift-check --rule-source`、`onboard --rule-files` 新增可重複的 `--exclude GLOB`（不含 `/` 比對檔名，含 `/` 比對相對於所給目錄的路徑），選到的每個檔都必須讀得成 rule file 或 Kubernetes manifest，不再依內容略過：recipe、registry、`Chart.yaml`、`groups` 拼錯、缺 `kind`、空檔都逐檔點名（`lint --ci` 結束碼 1，另兩支 2）；懸空 symlink、`--exclude` 排光也結束碼 2，副檔名不分大小寫。rule-packs 目錄改用新的 `--rule-pack-dir`（`lint`、`silencer-drift-check`），依唯一的宣告取 `rule-pack-*.yaml`，`validate-config --rule-packs` 與 CI 都經它；`operator-generate`、`operator-check`、`runtime-audit`、`rule-pack-split` 的 `--rule-packs-dir` 也是（`.yml` 也讀，其他 YAML 點名、結束碼 2）。`lint`、`onboard` 也讀 PrometheusRule 的 `spec`；`migrate-to-operator` 對已是 PrometheusRule 的物件不轉換、不算錯誤，於 stderr、checklist 與 `--json` 的 `already_crd` 點名。ConfigMap 頂層未知欄位（少縮排的 data key）點名；`binaryData` 折行的 base64 照 API server 的讀法接受；`da-crdecode` 對 `items: null` 的 list 也檢查外層原文差異。
