---
section: Fixed
topic: exporter
issues: [2418]
created: 2026-09-30T14:00:00+00:00
---
- **根 `_defaults.yaml` 同一層把新拼法寫成 null、舊拼法寫值時，`/metrics` 改送舊拼法的值（exporter / da-guard；[#2418](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2418)）**：例如 `mysql_threads_running: null` 加 `mysql_cpu: 30`，過去 `/metrics` 把 null 當成 0 並讓新拼法勝出而送出 `0`，`da-guard` 則把 null 視為沒寫、以 `30` 為準，於是把租戶自己寫的 `mysql_cpu: 30` 判成多餘，刪掉後 `/metrics` 從 30 變 0。現在根層與子目錄各層用同一條規則判斷「這一層有沒有寫某個拼法」（null 不算寫），同一層的「新拼法勝出」只在該層真的寫了的拼法之間比較，`/metrics` 送 `30`，`da-guard` 的刪除建議不再改變 `/metrics`，`diagnose` 的 `resolved` 也改為這個值。同一規則下，新拼法寫成 `.inf`／`-.inf`／`.nan`（不是閾值）且舊拼法有值時，`/metrics` 也由 ±Inf／NaN 改送舊拼法的值。file mode 的單檔設定同樣適用。根層某拼法沒寫值、而另一拼法也沒有值時行為不變（null 仍送 `0`）。
