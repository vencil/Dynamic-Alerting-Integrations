---
section: Fixed
topic: alertmanager-routing
issues: [2180]
created: 2026-09-27T17:00:14+00:00
---
- **receiver 必填欄位的值：Alertmanager 載入不了的值，schema、路由產生器與 Go guard 三方都擋（alertmanager-routing；[#2180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2180)）**：以前 email `to: [""]`、URL 或 `smarthost` 給 `" "`、必填欄位給清單（`url: [""]`）時，三方全部或部分放行，產生器照樣寫出，Alertmanager 拒收後**整份**設定 reload 失敗。現在 URL 與 `smarthost` 以 `tenant-config.schema.json` 的 `pattern` 定義格式（`http(s)://` 加 host；`host:port`，port 為數字）。必填欄位給字串以外的值一律報錯；`to` 清單的每一項必須是非空字串。Go guard 以前放行 `[]`、`0`、`false`，現在也擋。receiver `type` 不再正規化大小寫與前後空白，`Email`、` email` 三方都拒收（產生器以前會照常寫出）。da-tools 映像改為隨附這份 schema。
