---
section: Changed
topic: tenant-api
issues: [3267]
created: 2026-11-01T12:00:00+08:00
---
- **⚠️ Breaking：tenant-api 拒收 exporter 讀不了的所有門檻值（tenant-api；[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：寫入關卡改用 exporter 判定門檻值的同一個函式（da-guard `value_not_served` 用的也是它），不再只擋非有限數。`abc`、`70:high`、`disable:critical`、超出範圍的 `1e999`／`0x1p1024`、空字串，以及 `<key>_critical` 帶 `:severity` 的值，`PUT /tenants/{id}`、`/diff`、`/tenants/batch` 與 `/groups/{id}/batch`（兩種寫入模式）由 200 改為 400：`PUT`／`/diff` 是 `BAD_REQUEST`，batch patch 是 `INVALID_BODY`；`/validate` 回 `valid: false`。`default` 與每個時段的值都判。`70`、`1_000`、`0x1p4`、`70:critical`、`disable` 照收；寫成 `null` 的 key 不算寫入，照收；batch 只判自己寫的 key，檔案裡原有的不擋。custom alert 的 `threshold:` 原本就拒收同一組值，不變。
