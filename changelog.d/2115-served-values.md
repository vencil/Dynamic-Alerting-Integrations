---
section: Added
topic: exporter
issues: [2115]
created: 2026-09-28T02:35:21+00:00
---
- **`da-guard served-values`：以 JSON 印出 `/metrics` 對每個租戶實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：值由 exporter 自己的載入與解析算出（`defaults:`、根目錄平台檔的 `tenants:`、profile、子目錄都算在內），可用 `--at` 指定時間點。每個租戶輸出 `values`、`severities` 與 `unserved`（停用或不會發列的 key）；exporter 整份跳過的檔列在 `parse_failed`，此時 exit 3；兩個 key 產生同一條 `user_threshold` series（`/metrics` 會因此整份失敗）時 exit 2 並點名兩個 key。新增 `scripts/tools/_lib_tenant_values.py` 供 Python 讀取端呼叫，讀取端的切換在後續 PR。`da-tools guard served-values` 轉發到同一個子命令。
