---
section: Fixed
topic: da-tools
issues: [3519, 3477]
created: 2026-11-14T12:10:00+08:00
---
- **⚠️ breaking：對端回的不是 HTTP 時，工具走自己的「沒問到」路徑，不再 crash 或當成有結果（da-tools；[#3519](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3519)）**：`_lib_prometheus` 的 `http_get_json`／`http_post_json`／`probe_health`／`http_request_with_retry` 改接 `TRANSPORT_ERRORS`，`discover-instance-mappings --endpoint`、`check_trk_index_coverage.py --all-titles`、`generate_crd_schemas.py` 也改引用同一份清單。`--prometheus`／`--alertmanager` 指到非 HTTP 服務時，原本以 `internal_error` 結束（rc 2 加 traceback）的工具改印各自的具名錯誤（rc 2）。結束碼有變的三支：`threshold-govern`、`threshold-recommend` 原本 rc 0（前者把每個 key 列為 force_manual，後者回報「沒有樣本」），`batch-diagnose` 原本 rc 1（每個租戶記為 error），現在都是 rc 2。`http_request_with_retry` 對非 HTTP 回應會重試，耗盡後擲 `URLError`。錯誤訊息固定為一行 `BadStatusLine: <對端送來的前 200 字元>`。新增靜態守衛：包住請求的 `try` 若接了連線失敗卻漏掉 `http.client.HTTPException` 會讓測試轉紅。
