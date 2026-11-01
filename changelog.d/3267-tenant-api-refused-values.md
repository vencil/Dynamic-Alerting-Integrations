---
section: Changed
topic: tenant-api
issues: [3267]
created: 2026-11-01T12:00:00+08:00
---
- **⚠️ Breaking：tenant-api 拒收 exporter 讀不了的門檻值（tenant-api；[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：寫入關卡改用 exporter 判定門檻值的同一個函式（da-guard `value_not_served` 用的也是它），不再只擋非有限數。`abc`、`70:high`、`disable:critical`、超出範圍的 `1e999`／`0x1p1024`、空字串，以及 `<key>_critical` 帶 `:severity` 的值，由這次寫入放進去或改動時，由 200 改為：`PUT /tenants/{id}`、`/diff` 400 `BAD_REQUEST`，`/tenants/batch` 與 `/groups/{id}/batch` 該筆 op 失敗（422，`BAD_REQUEST`）。依 ADR-038 決定 12，檔案裡原本就有、exporter 讀起來沒變的（引號、空白、大小寫不同也算沒變）照收並回 `NOTICE:`；讀不到寫入前的檔案時照擋。`default` 與每個時段的值都判，custom-alerts 寫入同樣適用。非有限數照舊一律擋。`70`、`1_000`、`0x1p4`、`70:critical`、`disable`、`null` 照收。
