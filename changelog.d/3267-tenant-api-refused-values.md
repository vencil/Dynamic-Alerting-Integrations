---
section: Changed
topic: tenant-api
issues: [3267]
created: 2026-11-01T12:00:00+08:00
---
- **⚠️ Breaking：tenant-api 拒收 exporter 讀不了的門檻值（tenant-api；[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：`abc`、`70:high`、`disable:critical`、超出範圍的 `1e999`、不合數字文法的 `1_000`／`0x1p4`／尾數超過 20 位的數字（[#3457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3457)）、空字串，以及 `<key>_critical` 帶 `:severity` 的值（只有根 `defaults:` 給了 `<key>_critical`、且沒給 `<key>` 時，`90:critical` 照收；其餘情況整串必須是數字），判法與 `_silent_mode` 的值相同（ADR-038 決定 12）：這次寫入放進去或改動的，`PUT /tenants/{id}`、`/diff` 由 200 改為 400，`/tenants/batch` 與 `/groups/{id}/batch` 該筆 op 失敗（422）；檔案裡原有、exporter 讀起來沒變的（兩邊都以 exporter 的解碼讀，維度鍵換拼法也算沒變）照收並回 `NOTICE:`，`GET` 列在 `validation_notices`；讀不到寫入前的檔案時照擋。custom-alerts 寫入同樣適用；租戶選用的 profile 或平台檔 `tenants.<id>` 裡的這類值，寫入與 GET 回應多一則點名該檔的 notice，永不擋寫入。非有限數照舊一律擋；`70`、`1e3`、`70:critical`、`disable`、`null` 照收。exporter 載入時，每個這類值多印一行 `WARN: tenant=…` log。
