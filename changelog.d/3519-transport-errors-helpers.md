---
section: Fixed
topic: da-tools
issues: [3519, 3477]
created: 2026-11-14T12:10:00+08:00
---
- **⚠️ breaking：對端回的不是 HTTP 時，工具走自己的「沒問到」路徑（da-tools；[#3519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3519)）**：`_lib_prometheus` 的 `http_get_json`／`probe_health`，以及 `discover-instance-mappings --endpoint`、`check_trk_index_coverage.py --all-titles`、`generate_crd_schemas.py` 改接 `TRANSPORT_ERRORS`。`--prometheus`／`--alertmanager` 指到非 HTTP 服務時，原本以 `internal_error` 結束（rc 2 加 traceback）的工具改印各自的具名錯誤（rc 2）。結束碼有變的三支：`threshold-govern`、`threshold-recommend` 原本 rc 0，`batch-diagnose` 原本 rc 1，現在都是 rc 2。例外：`run_chaos_soak.py` 只修第一次探測（rc 2 具名）；soak 中途的 poll 遇到非 HTTP 回應仍以 internal error 結束。錯誤文字一律清理成一行：控制字元顯示為 `?`、例外自身的文字最多 200 字元，非 HTTP 回應另加型別名（如 `BadStatusLine: …`）；HTTP 錯誤的 reason、被拒絕的轉向位址也一樣。後兩支工具遇到 200 但內容不是預期格式時改為具名的 rc 2。移除：`_lib_prometheus.http_post_json` 與 `http_request_with_retry`（連同 `_lib_python` 的轉出口），已無呼叫端。新增靜態守衛：包住請求的 `try` 若接了連線失敗卻漏掉 `http.client.HTTPException` 會讓測試轉紅。
