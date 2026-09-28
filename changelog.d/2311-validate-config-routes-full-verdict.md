---
section: Fixed
topic: alertmanager-routing
issues: [2311]
created: 2026-09-28T12:40:35+00:00
---
- **validate-config 的 `routes` 列與 `generate-routes --validate` 共用整組判定（alertmanager-routing；[#2311](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2311)）**：以前這一列只檢查略過的項目與同名 receiver，`--validate` 會擋的 inhibit tripwire（壓掉 Watchdog、租戶靜音平台告警）、組裝時的平台不變式與 `amtool check-config` 拒收（例如 webhook URL `http://[1]/`），在這裡都是 PASS、結束碼 0。現在兩邊呼叫同一支判定：任一項成立即 FAIL、結束碼 1（租戶什麼都沒產生的樹例外：`--validate` 結束碼 1，這一列 WARN）。PATH 上沒有 `amtool` 時這一列改為 WARN（結束碼不變），明細註明未經 Alertmanager 驗證；`amtool` 跑不出結論時為結束碼 2。`--json` 的鍵與 `pass`／`warn`／`fail` 三值不變。
