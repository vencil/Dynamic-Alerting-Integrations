---
section: Fixed
topic: exporter
issues: [2681]
created: 2026-10-03T15:55:54+00:00
---
- **大型 YAML 的解析時間由平方級改為線性（threshold-exporter、da-guard、tenant-api；[#2681](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2681)）**：`gopkg.in/yaml.v3` v3.0.1 檢查重複 key 時兩兩比對，一個 mapping 的 key 很多時（例如頂層放很多 key 的 `_domain_policy.yaml`）會耗上數秒到數十秒；tenant-api 啟動、每次熱重載、PR 模式的每個寫入請求都會重新解析。改用 vendored 的 v3.0.1，只多套 grafana/go-yaml 的線性判重修正，解析結果與錯誤訊息不變。頂層 32000 個 key（約 300KB）的 `_domain_policy.yaml`：da-guard 由 20.8 秒降到 0.47 秒，`routingpolicy.UnmarshalPolicy` 由 3.4 秒降到 0.07 秒。
