---
section: Changed
topic: confd-family
issues: [3426]
created: 2026-10-31T01:30:00+00:00
---
- **⚠️ Breaking：rule file 工具讀哪些檔改由路徑宣告（[#3426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3426)）**：`lint`、`silencer-drift-check --rule-source`、`onboard --rule-files` 新增可重複的 `--exclude GLOB`（不含 `/` 比對檔名，含 `/` 比對相對於所給目錄的路徑），選到的每個檔都必須讀得成 rule file 或 Kubernetes manifest，不再依內容略過；目錄裡混有 recipe、registry、`Chart.yaml` 等非 rule file 的 YAML 時要以 `--exclude` 排除，否則逐檔點名（`lint --ci` 結束碼 1，另兩支 2）。`groups` 拼錯的 rule pack、缺 `kind` 的 manifest、`data:` 下少縮排成頂層 key 的 ConfigMap 欄位因此點名。`lint` 與 `onboard` 也讀 PrometheusRule 的 `spec`；`migrate-to-operator` 對已是 PrometheusRule 的物件不轉換、不算錯誤，於 stderr、checklist 與 `--json` 的 `already_crd` 點名。`binaryData` 折行的 base64 照 API server 的讀法接受；`da-crdecode` 對 `items: null` 的 list 也檢查 list 外層的原文差異。
