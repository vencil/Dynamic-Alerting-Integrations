---
section: Fixed
topic: exporter
issues: [2681]
created: 2026-10-03T15:55:54+00:00
---
- **大型 YAML 的解析時間由平方級改為線性（threshold-exporter、da-guard、tenant-api；[#2681](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2681)）**：`gopkg.in/yaml.v3` v3.0.1 檢查重複 key 時兩兩比對，一個 mapping 的 key 很多時會耗上數秒到數十秒，而 exporter 每次熱重載、tenant-api 每個寫入請求都會重新解析。改用 vendored 的上游 `go.yaml.in/yaml/v3` v3.0.5（`gopkg.in/yaml.v3` 改名後的同一套件），只多套 grafana/go-yaml 的線性判重修正，解析結果與錯誤訊息不變。
