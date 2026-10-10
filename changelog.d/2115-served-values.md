---
section: Added
topic: exporter
issues: [2115, 2750, 1516]
created: 2026-09-28T02:35:21+00:00
---
- **`da-guard served-values`：以 JSON 印出 `/metrics` 對每個租戶實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：值由 exporter 自己的載入與解析算出（含 `defaults:`、平台檔 `tenants:`、profile、子目錄），可用 `--at` 指定時間點。每個租戶輸出 `values`、`severities`、`unserved`（停用或不會發列的 key）、`dropped`（exporter 建不出 series 而丟掉的列）、`series`（每個閾值 key 在 `/metrics` 的 series 名稱、完整 label 與 `metric_key`，取自 `Gather`，#2750）、`state_filters`（開啟中各 state filter 的 severity，ADR-037）；加 `--schedules` 時另輸出 `schedules`（每個閾值 key 在 UTC 整天的逐時段值、severity 與 series，附 `expires`／`expired`；沒加時不計算）；頂層 `schema`（`da-guard.served-values/v1`，其他值或缺少即拒收）與 `aliases`（別名表）。哪些列會被收下，由 `/metrics` 同一組 collector 在私有 registry 上 `Gather` 決定。整份跳過的檔列在 `parse_failed`，讀不到的檔與子目錄列在 `unreadable`（原因 `stat_error`／`read_error`／`walk_error`），任一非空時 exit 3；讀了但不當租戶的檔列在 `skipped`；`--at` 那一刻 `Gather` 失敗或字串不是合法 UTF-8 時 exit 2，其他時段 `Gather` 失敗則該段帶 `error`、不影響結束碼。新增 `scripts/tools/_lib_tenant_values.py` 供 Python 讀取端呼叫；`da-tools guard served-values` 轉發到同一個子命令。
