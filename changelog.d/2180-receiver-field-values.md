---
section: Fixed
topic: alertmanager-routing
issues: [2180]
created: 2026-09-27T17:00:14+00:00
---
- **receiver 必填欄位的值改以 `tenant-config.schema.json` 為準檢查，schema 與路由產生器判定一致（alertmanager-routing；[#2180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2180)）**：以前 email `to: [""]`、URL 或 `smarthost` 給 `" "`、必填欄位給清單（`url: [""]`）時，schema 與產生器全部或部分放行，產生器照樣寫出，Alertmanager 拒收後**整份**設定 reload 失敗。現在 URL 與 `smarthost` 以 `tenant-config.schema.json` 的 `pattern` 定義格式（`http(s)://` 加 host；`host:port`，port 為數字）。必填欄位給字串以外的值一律報錯；`to` 清單的每一項必須是非空字串。哪些值 Alertmanager 實際拒收，以共享表 `receiver_presence_cases.json` 的 `am` 欄為準（以 Alertmanager `config.Load` 實跑斷言）。receiver `type` 不再正規化大小寫與前後空白，`Email`、` email` schema 與產生器都拒收（產生器以前會照常寫出），`notification_tester` 也不再把它們當成合法類型。da-tools 映像改為隨附這份 schema。
