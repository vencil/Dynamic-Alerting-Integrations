---
section: Changed
topic: exporter
issues: [3140]
created: 2026-10-20T23:30:00+00:00
---
- **da-guard served-values 分辨「值被拒」與「停用」（[#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)）**：JSON 契約擴充（schema 仍為 `da-guard.served-values/v1`）。`keys[].reason` 新增 `value_non_finite`、`value_unparsed`：resolver 讀到值但拒收、不送列，這不是停用，為它寫的告警永遠不會響（先前與拼錯的 key 同為 `no_row`）；`keys[].refused`（`non_finite`／`unparsed`）連同改送平台預設值的 base key 一起標出；`--schedules` 下值為 null 的時段帶 `reason`（`disabled`／`value_non_finite`／`value_unparsed`）。只在 resolver 自己記下拒收時才標，平台沒宣告的 key 仍是 `no_row`。根 `_defaults.yaml` 的 `defaults:` 寫成非有限數、而租戶自己沒寫的 key 新增 `keys[].reason` `root_default_non_finite`（`refused` 為 `non_finite`）：先前這種 key 不出現在 `keys`，讀取端分不出是被拿掉還是被拒收；它是維度鍵時 served-values 還會因 JSON 寫不出 NaN 而結束碼 2，現在恢復為 0。內附的 Python 讀取端（`_lib_tenant_values`）同步接受新值；validate-config 的 `unread_keys` 對這類 key 改寫出新原因。
