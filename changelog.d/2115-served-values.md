---
section: Added
topic: exporter
issues: [2115, 2750, 1516, 3016]
created: 2026-09-28T02:35:21+00:00
---
- **`da-guard served-values`：以 JSON 印出 `/metrics` 對每個租戶實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：值由 exporter 自己的載入與解析算出，可用 `--at` 指定時間點。每個租戶輸出 `values`、`severities`、`unserved`（停用或不會發列的 key）、`dropped`、`series`（#2750）、`state_filters`、`time_boxes`、`routing`（`_routing` 不是 `/metrics` 的值，另列）與 `keys`（每個 key 是否被 exporter 讀、沒讀的原因、登錄表宣告的其他讀取端；[ADR-038](docs/adr/038-unread-keys-judged-by-readers.md)）；`--schedules` 另輸出整天逐時段的 `schedules`。頂層 `schema`（`da-guard.served-values/v1`）與 `aliases`。哪些列被收下由 `/metrics` 同一組 collector 的 `Gather` 決定。整份跳過或讀不到的檔列在 `parse_failed`／`unreadable`（exit 3），不當租戶的檔列在 `skipped`；`--at` 那一刻 `Gather` 失敗或字串不是合法 UTF-8 時 exit 2（其他時段失敗只在該段帶 `error`）。新增 `scripts/tools/_lib_tenant_values.py` 供 Python 讀取端呼叫；`da-tools guard served-values` 轉發到同一個子命令。
