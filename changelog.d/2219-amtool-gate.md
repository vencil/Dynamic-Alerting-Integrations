---
section: Fixed
topic: alertmanager-routing
issues: [2219, 2260, 2506, 3689, 3793]
created: 2026-09-28T02:11:00+00:00
---
- **`generate-routes` 在寫出／套用前先問 Alertmanager 收不收（alertmanager-routing；[#2219](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2219)、[#3689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3689)）**：schema 放行、Alertmanager 卻拒收的值（例如 webhook URL `http://[1]/`、`http_config` 的布林欄位寫成字串、`oauth2` 沒有 `client_id`）以前照樣結束碼 0 寫出，`--apply` 也照推進。現在 `--output-configmap`、`--output-alertmanager`、`--apply` 與 `--validate`（組在內建預設 base 上，不是 `--base-config`；[#2260](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2260)）走同一道閘門：交給 Alertmanager 自己的設定載入器（`da-read`）判定，PATH 上有沒有 `amtool` 不影響結論（[#3793](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3793)）；拒收為結束碼 1、不寫檔也不 apply，讀不到結論（沒有 `da-read`）為結束碼 2。拒收訊息不印租戶寫的值。⚠️ breaking：`--validate` 對 Alertmanager 會拒收的設定改為結束碼 1。組裝時違反平台不變式（例如 inhibit 規則會讓租戶靜音平台告警，[#2506](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2506)）改為印 `FAIL:`、結束碼 1。`--apply` 之後 `/-/reload` 失敗由 WARN＋結束碼 0 改為結束碼 2。
