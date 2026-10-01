---
section: Added
topic: exporter
issues: [2115]
created: 2026-09-28T02:35:21+00:00
---
- **`da-guard served-values`：以 JSON 印出 `/metrics` 對每個租戶實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：值由 exporter 自己的載入與解析算出（`defaults:`、根目錄平台檔的 `tenants:`、profile、子目錄都算在內），可用 `--at` 指定時間點。每個租戶輸出 `values`、`severities`、`unserved`（停用或不會發列的 key）與 `dropped`（exporter 建不出 series 而丟掉的列）；哪些列會被收下，由 exporter `/metrics` 的同一組 collector 在私有 registry 上 `Gather` 決定。exporter 整份跳過的檔列在 `parse_failed`、stat 或讀取失敗而跳過的檔（例如權限不足、懸空 symlink；指向目錄的 symlink 不算）列在 `unreadable`（附封閉值原因 `stat_error`／`read_error`），任一非空時 exit 3；讀了但不當租戶的檔（檔名不以 `_` 開頭、沒有 `tenants:` 或其為空）列在 `skipped`，附原因；`Gather` 失敗（例如兩個 key 產生同一條 series，`/metrics` 會整份回 500）或 tenant id／key 不是合法 UTF-8 時 exit 2；exit 2 以 production `/metrics` 同一組 collector 的 `Gather` 為準。新增 `scripts/tools/_lib_tenant_values.py` 供 Python 讀取端呼叫。`da-tools guard served-values` 轉發到同一個子命令。
