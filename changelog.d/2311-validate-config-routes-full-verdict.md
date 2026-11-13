---
section: Fixed
topic: alertmanager-routing
issues: [2311, 3689, 3793]
created: 2026-09-28T12:40:35+00:00
---
- **validate-config 的 `routes` 列與 `generate-routes --validate` 共用整組判定（alertmanager-routing；[#2311](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2311)）**：以前這一列只檢查略過的項目與同名 receiver，`--validate` 會擋的 inhibit tripwire（壓掉 Watchdog、租戶靜音平台告警）、組裝時的平台不變式與 Alertmanager 拒收（例如 webhook URL `http://[1]/`），在這裡都是 PASS、結束碼 0。現在兩邊呼叫同一支判定：任一項成立即 FAIL、結束碼 1（租戶什麼都沒產生的樹例外：`--validate` 結束碼 1，這一列 WARN）。Alertmanager 的判定由它的設定載入器（`da-read`）給（[#3689](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3689)），與 PATH 上有沒有 `amtool` 無關；讀不到 Alertmanager 的結論時為結束碼 2。`--json` 的鍵與 `pass`／`warn`／`fail` 三值不變。
