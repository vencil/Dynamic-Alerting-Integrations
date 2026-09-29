---
section: Fixed
topic: alertmanager-routing
issues: [2219, 2260]
created: 2026-09-28T02:11:00+00:00
---
- **`generate-routes` 在寫出／套用前交給 Alertmanager 自己的 parser 驗證（alertmanager-routing；[#2219](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2219)）**：schema 放行、Alertmanager 卻拒收的值（例如 webhook URL `http://[1]/`、host 含 U+0085）以前照樣結束碼 0 寫出，`--apply` 也照推進叢集。現在 PATH 上有 `amtool` 時，`--output-configmap` 與 `--apply` 會對實際要寫出／套用的 `alertmanager.yml` 跑 `amtool check-config`：拒收則結束碼 1、不寫檔、不 apply；`--validate` 在其餘檢查通過後，也把設定組進內建預設 base（不是 `--base-config`）交給同一道驗證（[#2260](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2260)）；`amtool` 無法執行、逾時或自身出錯（沒有給出拒收判定）為結束碼 2。沒有 `amtool` 時行為不變，但 stderr 會印 NOTICE 說明未經 Alertmanager 驗證；fragment 模式一律印 NOTICE（fragment 不是完整設定，無法驗證）。組裝時違反平台不變式（例如 base 的 inhibit 規則會讓租戶靜音平台告警）改為印 `FAIL:`、結束碼 1，不再噴 traceback。`--apply` 之後 `/-/reload` 失敗由 WARN＋結束碼 0 改為結束碼 2。
