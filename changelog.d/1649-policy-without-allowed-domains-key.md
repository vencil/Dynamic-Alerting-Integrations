---
section: Changed
topic: alertmanager-routing
issues: [1649]
created: 2026-10-10T13:20:00+08:00
---
- **`--policy` 的檔沒寫 `allowed_domains` 鍵時結束碼 2，不再當成「不限制」（`generate-routes`、`validate-config`；[#1649](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1649)）**：行為變更。原本空檔、只有註解、鍵拼錯（如 `allowed_domain:`）、清單全是非字串，都讓 webhook 網域白名單整個關掉，結束碼 0 並印 `[PASS] policy`，與不給 `--policy` 分不出來。現在這幾種都回 2，鍵拼錯時訊息會點名；清單裡只要有一項不是字串也回 2（原本混合清單會默默丟掉那幾項）。`allowed_domains: []` 與 `allowed_domains:`（空值）仍是不限制。⚠️ 只寫 lint 規則（`denied_functions` 等）的 policy 檔若也交給這兩支，要補一行 `allowed_domains: []`。
